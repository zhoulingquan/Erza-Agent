"""OpenAI-compatible provider for all non-Anthropic LLM APIs."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import json_repair
from loguru import logger

from erza.providers._openai_compat_helpers import (
    _ALLOWED_MSG_KEYS,
    _DEFAULT_OPENROUTER_HEADERS,
    _KIMI_THINKING_MODELS,
    _RESPONSES_FAILURE_THRESHOLD,
    _RESPONSES_PROBE_INTERVAL_S,
    _deep_merge,
    _extract_text_content,
    _gateway_reasoning_extra_body,
    _get,
    _is_direct_openai_base,
    _is_local_endpoint,
    _merge_responses_extra_body,
    _model_slug,
    _model_thinking_style,
    _openai_compat_timeout_s,
    _parse_response,
    _parse_stream_chunks,
    _responses_circuit_key,
    _short_tool_id,
    _thinking_extra_body,
    _thinking_styles_for,
    _uses_openrouter_attribution,
)
from erza.providers.base import LLMProvider, LLMResponse
from erza.providers.openai_responses import (
    consume_sdk_stream,
    convert_messages,
    convert_tools,
    parse_response_output,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI as AsyncOpenAIType

    from erza.providers.registry import ProviderSpec

# Module-level placeholder — set lazily by _ensure_client on first real
# use, or replaced by tests via ``patch(...)``.  Kept as a plain name so
# that ``unittest.mock.patch`` can find and replace it.
AsyncOpenAI: Any = None


class OpenAICompatProvider(LLMProvider):
    """Unified provider for all OpenAI-compatible APIs.

    Receives a resolved ``ProviderSpec`` from the caller — no internal
    registry lookups needed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "gpt-4o",
        extra_headers: dict[str, str] | None = None,
        spec: ProviderSpec | None = None,
        extra_body: dict[str, Any] | None = None,
        api_type: str = "auto",
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self._spec = spec
        self._extra_body = extra_body or {}
        # api_type 仅对直连 OpenAI 兼容端点生效（custom / 未注册 provider）；
        # 内置 provider 固定 chat_completions，忽略传入的 api_type。
        _is_direct = spec is None or spec.is_direct
        self._api_type = api_type if _is_direct else "auto"

        if api_key and spec and spec.env_key:
            self._setup_env(api_key, api_base)

        effective_base = api_base or (spec.default_api_base if spec else None) or None
        self._effective_base = effective_base
        self._default_headers = {"x-session-affinity": uuid.uuid4().hex}
        if spec and spec.extra_headers:
            self._default_headers.update(spec.extra_headers)
        elif _uses_openrouter_attribution(spec, effective_base):
            self._default_headers.update(_DEFAULT_OPENROUTER_HEADERS)
        if extra_headers:
            self._default_headers.update(extra_headers)
        self._api_key_for_client = api_key or "no-key"
        self._is_local = _is_local_endpoint(spec, effective_base)

        # Lazy-init: the OpenAI client and its httpx transport are expensive
        # to create (~700 ms on Windows). Defer until first use.
        self._client: AsyncOpenAIType | None = None
        self._client_lock = asyncio.Lock()

        # Responses API circuit breaker: skip after repeated failures,
        # probe again after _RESPONSES_PROBE_INTERVAL_S seconds.
        self._responses_failures: dict[str, int] = {}
        self._responses_tripped_at: dict[str, float] = {}

    @property
    def is_local(self) -> bool:
        """是否为本地端点(localhost/127.0.0.1 等)。"""
        return self._is_local

    def _build_client(self) -> None:
        """Create the OpenAI client using the current module-level AsyncOpenAI."""
        import httpx

        timeout_s = _openai_compat_timeout_s()
        http_client: httpx.AsyncClient | None = None
        if self._is_local:
            # Local model servers (Ollama, llama.cpp, vLLM) often close idle
            # HTTP connections before the client-side keepalive expires. When
            # two LLM calls happen seconds apart (e.g. heartbeat _decide then
            # process_direct), the second call may grab a now-dead pooled
            # connection, causing a transient APIConnectionError on every first
            # attempt. Disabling keepalive for local endpoints avoids this by
            # opening a fresh connection for each request, which is cheap on a
            # LAN. Cloud providers benefit from keepalive, so we leave the
            # default pool settings for them.
            http_client = httpx.AsyncClient(
                limits=httpx.Limits(keepalive_expiry=0),
                timeout=timeout_s,
            )
        self._client = AsyncOpenAI(
            api_key=self._api_key_for_client,
            base_url=self._effective_base,
            default_headers=self._default_headers,
            max_retries=0,
            timeout=timeout_s,
            http_client=http_client,
        )

    async def _ensure_client(self):
        """Return the shared OpenAI client, creating it on first call."""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            global AsyncOpenAI
            if AsyncOpenAI is None:
                if os.environ.get("LANGFUSE_SECRET_KEY") and importlib.util.find_spec("langfuse"):
                    from langfuse.openai import AsyncOpenAI as _AsyncOpenAI
                else:
                    if os.environ.get("LANGFUSE_SECRET_KEY"):
                        logger.warning(
                            "LANGFUSE_SECRET_KEY is set but langfuse is not installed; "
                            "install with `pip install langfuse` to enable tracing"
                        )
                    from openai import AsyncOpenAI as _AsyncOpenAI
                AsyncOpenAI = _AsyncOpenAI

            self._build_client()
            return self._client

    async def aclose(self) -> None:
        """关闭底层 httpx 连接池,释放 socket/文件描述符资源。

        在网关停止或 provider 重新加载时调用,避免连接泄漏导致
        ``Too many open files`` 错误。重复调用是安全的(幂等)。
        """
        client = self._client
        if client is None:
            return
        # 先置 None,避免并发场景下其他协程在关闭过程中误用。
        self._client = None
        try:
            await client.close()
        except Exception as exc:
            logger.warning("关闭 OpenAI 客户端连接池失败: {}", exc)
        # 清理熔断器状态字典,释放残留的 (model, reasoning_effort) 条目。
        # 这些字典在 provider 实例销毁后无意义,显式清理避免延迟释放。
        self._responses_failures.clear()
        self._responses_tripped_at.clear()

    def _setup_env(self, api_key: str, api_base: str | None) -> None:
        """Set environment variables based on provider spec."""
        spec = self._spec
        if not spec or not spec.env_key:
            return
        os.environ.setdefault(spec.env_key, api_key)
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key).replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    @classmethod
    def _apply_cache_control(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Inject cache_control markers for prompt caching."""
        cache_marker = {"type": "ephemeral"}
        new_messages = list(messages)

        def _mark(msg: dict[str, Any]) -> dict[str, Any]:
            content = msg.get("content")
            if isinstance(content, str):
                return {
                    **msg,
                    "content": [
                        {"type": "text", "text": content, "cache_control": cache_marker},
                    ],
                }
            if isinstance(content, list) and content:
                nc = list(content)
                nc[-1] = {**nc[-1], "cache_control": cache_marker}
                return {**msg, "content": nc}
            return msg

        if new_messages and new_messages[0].get("role") == "system":
            new_messages[0] = _mark(new_messages[0])
        if len(new_messages) >= 3:
            new_messages[-2] = _mark(new_messages[-2])

        new_tools = tools
        if tools:
            new_tools = list(tools)
            for idx in cls._tool_cache_marker_indices(new_tools):
                new_tools[idx] = {**new_tools[idx], "cache_control": cache_marker}
        return new_messages, new_tools

    @staticmethod
    def _normalize_tool_call_id(tool_call_id: Any) -> Any:
        """Normalize to a provider-safe 9-char alphanumeric form."""
        if not isinstance(tool_call_id, str):
            return tool_call_id
        if len(tool_call_id) == 9 and tool_call_id.isalnum():
            return tool_call_id
        return hashlib.sha1(tool_call_id.encode()).hexdigest()[:9]

    def _should_normalize_tool_call_ids(self) -> bool:
        """Return True for providers that reject normal OpenAI tool call IDs."""
        return bool(self._spec and self._spec.normalize_tool_call_ids)

    @staticmethod
    def _normalize_tool_call_arguments(arguments: Any) -> str:
        """Force function.arguments into a valid JSON object string."""
        if isinstance(arguments, str):
            stripped = arguments.strip()
            if not stripped:
                return "{}"
            try:
                parsed = json_repair.loads(stripped)
            except Exception:
                return "{}"
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False)
            return "{}"
        if isinstance(arguments, dict):
            return json.dumps(arguments, ensure_ascii=False)
        return "{}"

    @staticmethod
    def _coerce_content_to_string(content: Any) -> str | None:
        """Coerce block/list content into plain text for strict string-only APIs."""
        if content is None or isinstance(content, str):
            return content
        text = _extract_text_content(content)
        if isinstance(text, str) and text:
            return text
        try:
            dumped = json.dumps(content, ensure_ascii=False)
        except Exception:
            dumped = str(content)
        return dumped or "(empty)"

    def _sanitize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip non-standard keys, normalize tool_call IDs."""
        sanitized = LLMProvider._sanitize_request_messages(messages, _ALLOWED_MSG_KEYS)
        id_map: dict[str, str] = {}
        pending_tool_ids: dict[str, deque[str]] = {}
        force_string_content = bool(self._spec and self._spec.force_string_content)
        normalize_tool_ids = self._should_normalize_tool_call_ids()

        def map_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            if not normalize_tool_ids:
                return value
            return id_map.setdefault(value, self._normalize_tool_call_id(value))

        def unique_tool_id(value: Any, used_ids: set[str], idx: int) -> str:
            if isinstance(value, str) and value:
                base = map_id(value)
            else:
                base = _short_tool_id()
            if not isinstance(base, str) or not base:
                base = _short_tool_id()
            if base not in used_ids:
                return base
            seed = value if isinstance(value, str) and value else base
            salt = 1
            while True:
                candidate = self._normalize_tool_call_id(f"{seed}:{idx}:{salt}")
                if isinstance(candidate, str) and candidate not in used_ids:
                    return candidate
                salt += 1

        def map_tool_result_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            queue = pending_tool_ids.get(value)
            if queue:
                mapped = queue.popleft()
                if not queue:
                    pending_tool_ids.pop(value, None)
                return mapped
            return map_id(value)

        for clean in sanitized:
            if isinstance(clean.get("tool_calls"), list):
                normalized = []
                used_ids: set[str] = set()
                for idx, tc in enumerate(clean["tool_calls"]):
                    if not isinstance(tc, dict):
                        normalized.append(tc)
                        continue
                    tc_clean = dict(tc)
                    raw_id = tc_clean.get("id")
                    mapped_id = unique_tool_id(raw_id, used_ids, idx)
                    tc_clean["id"] = mapped_id
                    used_ids.add(mapped_id)
                    if isinstance(raw_id, str) and raw_id:
                        pending_tool_ids.setdefault(raw_id, deque()).append(mapped_id)
                    function = tc_clean.get("function")
                    if isinstance(function, dict):
                        function_clean = dict(function)
                        if "arguments" in function_clean:
                            function_clean["arguments"] = self._normalize_tool_call_arguments(
                                function_clean.get("arguments")
                            )
                        else:
                            function_clean["arguments"] = "{}"
                        tc_clean["function"] = function_clean
                    normalized.append(tc_clean)
                clean["tool_calls"] = normalized
                if clean.get("role") == "assistant":
                    # Some OpenAI-compatible gateways reject assistant messages
                    # that mix non-empty content with tool_calls.
                    clean["content"] = None
            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_tool_result_id(clean["tool_call_id"])
            if force_string_content and not (
                clean.get("role") == "assistant" and clean.get("tool_calls")
            ):
                clean["content"] = self._coerce_content_to_string(clean.get("content"))
        return self._enforce_role_alternation(sanitized)

    # ------------------------------------------------------------------
    # Build kwargs
    # ------------------------------------------------------------------

    @staticmethod
    def _supports_temperature(
        model_name: str,
        reasoning_effort: str | None = None,
    ) -> bool:
        """Return True when the model accepts a temperature parameter.

        GPT-5 family and reasoning models (o1/o3/o4) reject temperature
        when reasoning_effort is set to anything other than ``"none"``.
        """
        if reasoning_effort and reasoning_effort.lower() != "none":
            return False
        name = model_name.lower()
        return not any(token in name for token in ("gpt-5", "o1", "o3", "o4"))

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        model_name = model or self.default_model
        spec = self._spec

        if spec and spec.supports_prompt_caching:
            if any(model_name.lower().startswith(k) for k in ("anthropic/", "claude")):
                messages, tools = self._apply_cache_control(messages, tools)

        if spec and spec.strip_model_prefix:
            model_name = model_name.split("/")[-1]

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": self._sanitize_messages(self._sanitize_empty_content(messages)),
        }

        # GPT-5 and reasoning models (o1/o3/o4) reject temperature when
        # reasoning_effort is active.  Only include it when safe.
        if self._supports_temperature(model_name, reasoning_effort):
            kwargs["temperature"] = temperature

        if spec and getattr(spec, "supports_max_completion_tokens", False):
            kwargs["max_completion_tokens"] = max(1, max_tokens)
        else:
            kwargs["max_tokens"] = max(1, max_tokens)

        if spec:
            model_lower = model_name.lower()
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    break

        # Normalize reasoning_effort into a semantic form (OpenAI vocab)
        # used for internal decisions, and a wire form actually sent out.
        # "minimum" is accepted as a DashScope-native alias for "minimal".
        semantic_effort: str | None = None
        if isinstance(reasoning_effort, str):
            semantic_effort = reasoning_effort.lower()
            if semantic_effort == "minimum":
                semantic_effort = "minimal"

        wire_effort = reasoning_effort
        if spec and spec.reasoning_effort_aliases and semantic_effort:
            # 部分 provider 不接受 "minimal"，需转为 wire 格式别名（如 DashScope minimum）
            for semantic, wire in spec.reasoning_effort_aliases:
                if semantic_effort == semantic:
                    wire_effort = wire
                    break

        if wire_effort and semantic_effort != "none":
            kwargs["reasoning_effort"] = wire_effort

        # Only send thinking controls when reasoning_effort is explicit so
        # omitting the config preserves each provider's default.
        if reasoning_effort is not None:
            thinking_enabled = semantic_effort not in ("none", "minimal")
            for thinking_style in _thinking_styles_for(spec, model_name):
                extra = _thinking_extra_body(thinking_style, thinking_enabled)
                if extra:
                    kwargs.setdefault("extra_body", {}).update(extra)
            gateway_style = getattr(spec, "gateway_reasoning_style", "") if spec else ""
            if gateway_style and _model_thinking_style(model_name):
                extra = _gateway_reasoning_extra_body(gateway_style, semantic_effort)
                if extra:
                    kwargs.setdefault("extra_body", {}).update(extra)

            # Moonshot rejects requests that carry both 'reasoning_effort'
            # and the native 'thinking' param.  We already expressed the
            # user's intent via the provider-native shape, so drop the
            # redundant wire-level kwarg.  Only kimi models need this —
            # Xiaomi's API accepts both params.
            if _model_slug(model_name) in _KIMI_THINKING_MODELS:
                kwargs.pop("reasoning_effort", None)

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        # Backfill reasoning_content="" on assistants missing it: DeepSeek
        # thinking mode rejects history otherwise (#3554, #3584); "" reads
        # as "no thinking that turn". DeepSeek-V4/reasoner reason natively,
        # so backfill even without explicit reasoning_effort.
        explicit_thinking = (
            reasoning_effort is not None
            and semantic_effort not in ("none", "minimal")
            and ((spec and spec.thinking_style) or _model_thinking_style(model_name))
        )
        implicit_deepseek_thinking = (
            spec is not None
            and spec.backfill_reasoning_content
            and semantic_effort not in ("none", "minimal", "minimum")
            and any(t in model_name.lower() for t in ("deepseek-v4", "deepseek-reasoner"))
        )
        if explicit_thinking or implicit_deepseek_thinking:
            for msg in kwargs["messages"]:
                if msg.get("role") == "assistant" and "reasoning_content" not in msg:
                    msg["reasoning_content"] = ""

        # Merge user-configured extra_body last so it can override or
        # extend provider-specific defaults (e.g. chat_template_kwargs,
        # guided_json, repetition_penalty).  Uses recursive merge so
        # nested dicts like {"chat_template_kwargs": {"enable_thinking": false}}
        # do not clobber sibling keys already set by thinking-style logic.
        if self._extra_body:
            existing = kwargs.get("extra_body", {})
            kwargs["extra_body"] = _deep_merge(existing, self._extra_body)

        return kwargs

    def _should_use_responses_api(
        self,
        model: str | None,
        reasoning_effort: str | None,
    ) -> bool:
        """Use Responses API only for direct OpenAI requests that benefit from it."""
        if self._api_type == "chat_completions":
            return False
        # 仅直连 OpenAI 官方端点（custom provider 指向 api.openai.com，或未注册
        # provider 但 base 是 api.openai.com）才考虑 Responses API；其他 provider
        # （deepseek/opencode/agnes 或 custom 指向第三方网关）一律走 chat_completions。
        if not _is_direct_openai_base(self._effective_base):
            return False
        if self._api_type == "responses":
            # Explicit configuration means Responses is mandatory; do not
            # consult the circuit breaker or fall back to Chat Completions.
            return True

        model_name = (model or self.default_model).lower()
        wants = False
        if reasoning_effort and reasoning_effort.lower() != "none":
            wants = True
        elif any(token in model_name for token in ("gpt-5", "o1", "o3", "o4")):
            wants = True
        if not wants:
            return False

        return self._responses_circuit_allows_probe(model, reasoning_effort)

    def _responses_circuit_allows_probe(
        self,
        model: str | None,
        reasoning_effort: str | None,
    ) -> bool:
        """Return False when the Responses API circuit breaker is open."""
        key = _responses_circuit_key(model, self.default_model, reasoning_effort)
        failures = self._responses_failures.get(key, 0)
        if failures >= _RESPONSES_FAILURE_THRESHOLD:
            tripped = self._responses_tripped_at.get(key, 0.0)
            if (time.monotonic() - tripped) < _RESPONSES_PROBE_INTERVAL_S:
                return False
            # Half-open: allow one probe attempt
        return True

    def _record_responses_failure(self, model: str | None, reasoning_effort: str | None) -> None:
        key = _responses_circuit_key(model, self.default_model, reasoning_effort)
        count = self._responses_failures.get(key, 0) + 1
        self._responses_failures[key] = count
        if count >= _RESPONSES_FAILURE_THRESHOLD:
            self._responses_tripped_at[key] = time.monotonic()
            logger.warning(
                "Responses API circuit open for {} — falling back to Chat Completions",
                key,
            )

    def _record_responses_success(self, model: str | None, reasoning_effort: str | None) -> None:
        # 成功时清理对应 key,将熔断器从 open/half-open 重置为 closed。
        # 同时清理 _responses_tripped_at,避免 tripped_at 残留导致下次
        # 调用时误判为已跳闸(尽管 _responses_circuit_open 会先检查 failures,
        # 但显式清理更安全,防止状态不一致)。
        key = _responses_circuit_key(model, self.default_model, reasoning_effort)
        self._responses_failures.pop(key, None)
        self._responses_tripped_at.pop(key, None)

    @staticmethod
    def _should_fallback_from_responses_error(e: Exception) -> bool:
        """Fallback only for likely Responses API compatibility errors."""
        response = getattr(e, "response", None)
        status_code = getattr(e, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        if status_code not in {400, 404, 422}:
            return False

        body = (
            getattr(e, "body", None) or getattr(e, "doc", None) or getattr(response, "text", None)
        )
        body_text = str(body).lower() if body is not None else ""
        compatibility_markers = (
            "responses",
            "response api",
            "max_output_tokens",
            "instructions",
            "previous_response",
            "unsupported",
            "not supported",
            "unknown parameter",
            "unrecognized request argument",
        )
        return any(marker in body_text for marker in compatibility_markers)

    def _build_responses_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build a Responses API body for direct OpenAI requests."""
        model_name = model or self.default_model
        if self._spec and self._spec.strip_model_prefix:
            model_name = model_name.split("/")[-1]
        sanitized_messages = self._sanitize_messages(self._sanitize_empty_content(messages))
        instructions, input_items = convert_messages(sanitized_messages)

        body: dict[str, Any] = {
            "model": model_name,
            "instructions": instructions or None,
            "input": input_items,
            "max_output_tokens": max(1, max_tokens),
            "store": False,
            "stream": False,
        }

        if self._supports_temperature(model_name, reasoning_effort):
            body["temperature"] = temperature

        if reasoning_effort and reasoning_effort.lower() != "none":
            body["reasoning"] = {"effort": reasoning_effort}
            body["include"] = ["reasoning.encrypted_content"]

        if tools:
            body["tools"] = convert_tools(tools)
            body["tool_choice"] = tool_choice or "auto"

        extra_body = getattr(self, "_extra_body", {})
        if extra_body:
            body = _merge_responses_extra_body(body, extra_body)

        return body

    # ------------------------------------------------------------------
    # Response parsing (bodies live in _openai_compat_helpers)
    # ------------------------------------------------------------------

    def _parse(self, response: Any) -> LLMResponse:
        """Parse a single non-streaming response (delegated to helpers)."""
        return _parse_response(response, getattr(self, "_spec", None))

    @classmethod
    def _parse_chunks(cls, chunks: list[Any]) -> LLMResponse:
        """Parse accumulated streaming chunks (delegated to helpers)."""
        return _parse_stream_chunks(chunks)

    @classmethod
    def _extract_error_metadata(cls, e: Exception) -> dict[str, Any]:
        response = getattr(e, "response", None)
        headers = getattr(response, "headers", None)
        payload = (
            getattr(e, "body", None) or getattr(e, "doc", None) or getattr(response, "text", None)
        )
        if payload is None and response is not None:
            response_json = getattr(response, "json", None)
            if callable(response_json):
                try:
                    payload = response_json()
                except Exception:
                    payload = None
        error_type, error_code = LLMProvider._extract_error_type_code(payload)

        status_code = getattr(e, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        should_retry: bool | None = None
        if headers is not None:
            raw = headers.get("x-should-retry")
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered == "true":
                    should_retry = True
                elif lowered == "false":
                    should_retry = False

        error_kind: str | None = None
        error_name = e.__class__.__name__.lower()
        if "timeout" in error_name:
            error_kind = "timeout"
        elif "connection" in error_name:
            error_kind = "connection"

        return {
            "error_status_code": int(status_code) if status_code is not None else None,
            "error_kind": error_kind,
            "error_type": error_type,
            "error_code": error_code,
            "error_retry_after_s": cls._extract_retry_after_from_headers(headers),
            "error_should_retry": should_retry,
        }

    @staticmethod
    def _handle_error(
        e: Exception,
        *,
        spec: ProviderSpec | None = None,
        api_base: str | None = None,
    ) -> LLMResponse:
        body = (
            getattr(e, "doc", None)
            or getattr(e, "body", None)
            or getattr(getattr(e, "response", None), "text", None)
        )
        body_text = body if isinstance(body, str) else str(body) if body is not None else ""
        msg = (
            f"Error: {body_text.strip()[:500]}" if body_text.strip() else f"Error calling LLM: {e}"
        )

        text = f"{body_text} {e}".lower()
        if spec and spec.is_local and ("502" in text or "connection" in text or "refused" in text):
            msg += (
                "\nHint: this is a local model endpoint. Check that the local server is reachable at "
                f"{api_base or spec.default_api_base}, and if you are using a proxy/tunnel, make sure it "
                "can reach your local Ollama/vLLM service instead of routing localhost through the remote host."
            )

        response = getattr(e, "response", None)
        retry_after = LLMProvider._extract_retry_after_from_headers(
            getattr(response, "headers", None)
        )
        if retry_after is None:
            retry_after = LLMProvider._extract_retry_after(msg)
        return LLMResponse(
            content=msg,
            finish_reason="error",
            retry_after=retry_after,
            **OpenAICompatProvider._extract_error_metadata(e),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        await self._ensure_client()
        try:
            if self._should_use_responses_api(model, reasoning_effort):
                try:
                    body = self._build_responses_body(
                        messages,
                        tools,
                        model,
                        max_tokens,
                        temperature,
                        reasoning_effort,
                        tool_choice,
                    )
                    result = parse_response_output(await self._client.responses.create(**body))
                    self._record_responses_success(model, reasoning_effort)
                    return result
                except Exception as responses_error:
                    if self._api_type == "responses":
                        raise
                    if not self._should_fallback_from_responses_error(responses_error):
                        raise
                    self._record_responses_failure(model, reasoning_effort)

            kwargs = self._build_kwargs(
                messages,
                tools,
                model,
                max_tokens,
                temperature,
                reasoning_effort,
                tool_choice,
            )
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return self._handle_error(e, spec=self._spec, api_base=self.api_base)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        await self._ensure_client()
        idle_timeout_s = int(os.environ.get("ERZA_STREAM_IDLE_TIMEOUT_S", "90"))
        try:
            if self._should_use_responses_api(model, reasoning_effort):
                try:
                    body = self._build_responses_body(
                        messages,
                        tools,
                        model,
                        max_tokens,
                        temperature,
                        reasoning_effort,
                        tool_choice,
                    )
                    body["stream"] = True
                    stream = await self._client.responses.create(**body)

                    async def _timed_stream():
                        stream_iter = stream.__aiter__()
                        while True:
                            try:
                                yield await asyncio.wait_for(
                                    stream_iter.__anext__(),
                                    timeout=idle_timeout_s,
                                )
                            except StopAsyncIteration:
                                break

                    (
                        content,
                        tool_calls,
                        finish_reason,
                        usage,
                        reasoning_content,
                    ) = await consume_sdk_stream(
                        _timed_stream(),
                        on_content_delta,
                        on_tool_call_delta=on_tool_call_delta,
                    )
                    self._record_responses_success(model, reasoning_effort)
                    return LLMResponse(
                        content=content or None,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                        usage=usage,
                        reasoning_content=reasoning_content,
                    )
                except Exception as responses_error:
                    if self._api_type == "responses":
                        raise
                    if not self._should_fallback_from_responses_error(responses_error):
                        raise
                    self._record_responses_failure(model, reasoning_effort)

            kwargs = self._build_kwargs(
                messages,
                tools,
                model,
                max_tokens,
                temperature,
                reasoning_effort,
                tool_choice,
            )
            if self._spec and self._spec.stream_extra_body and tools and on_tool_call_delta:
                # 部分 provider（如 Z.AI/GLM）需要显式 flag 才能在流式响应中
                # 返回 tool-call 参数。通过 extra_body 注入（由 registry 声明）。
                for k, v in self._spec.stream_extra_body.items():
                    kwargs.setdefault("extra_body", {})[k] = v
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            stream = await self._client.chat.completions.create(**kwargs)
            chunks: list[Any] = []
            stream_iter = stream.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=idle_timeout_s,
                    )
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
                if chunk.choices:
                    delta_obj = chunk.choices[0].delta
                    if on_content_delta:
                        text = getattr(delta_obj, "content", None)
                        if text:
                            await on_content_delta(text)
                    if on_thinking_delta:
                        reasoning = getattr(delta_obj, "reasoning_content", None) or getattr(
                            delta_obj,
                            "reasoning",
                            None,
                        )
                        r_text = _extract_text_content(reasoning)
                        if r_text:
                            await on_thinking_delta(r_text)
                    if on_tool_call_delta:
                        for idx, tool_delta in enumerate(
                            getattr(delta_obj, "tool_calls", None) or []
                        ):
                            fn = _get(tool_delta, "function")
                            tool_index = _get(tool_delta, "index")
                            await on_tool_call_delta(
                                {
                                    "index": tool_index if tool_index is not None else idx,
                                    "call_id": str(_get(tool_delta, "id") or ""),
                                    "name": str(_get(fn, "name") or "") if fn is not None else "",
                                    "arguments_delta": (
                                        str(_get(fn, "arguments") or "") if fn is not None else ""
                                    ),
                                }
                            )
                        function_call = getattr(delta_obj, "function_call", None)
                        if function_call:
                            await on_tool_call_delta(
                                {
                                    "index": 0,
                                    "call_id": "",
                                    "name": str(_get(function_call, "name") or ""),
                                    "arguments_delta": str(_get(function_call, "arguments") or ""),
                                }
                            )
            return self._parse_chunks(chunks)
        except asyncio.TimeoutError:
            return LLMResponse(
                content=(
                    f"Error calling LLM: stream stalled for more than {idle_timeout_s} seconds"
                ),
                finish_reason="error",
                error_kind="timeout",
            )
        except Exception as e:
            return self._handle_error(e, spec=self._spec, api_base=self.api_base)

    def get_default_model(self) -> str:
        return self.default_model
