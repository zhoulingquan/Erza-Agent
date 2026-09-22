"""Pure helpers for the OpenAI-compatible provider.

Module-level constants and stateless functions extracted from
``erza.providers.openai_compat_provider`` (model-name thinking-style maps,
request timeouts, tool-call parsing and coercion, dict merging, Responses
API body builders, local-endpoint detection). Nothing here touches provider
instance state, so it is importable and unit-testable standalone.

``openai_compat_provider`` imports back what the provider class uses;
tests keep importing private names from either module.
"""

from __future__ import annotations

import json
import os
import secrets
import string
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import json_repair
from loguru import logger

from erza.providers.base import LLMResponse, ToolCallRequest

if TYPE_CHECKING:
    from erza.providers.registry import ProviderSpec

_ALLOWED_MSG_KEYS = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "reasoning_content",
        "extra_content",
    }
)
_ALNUM = string.ascii_letters + string.digits

_STANDARD_TC_KEYS = frozenset({"id", "type", "index", "function"})
_STANDARD_FN_KEYS = frozenset({"name", "arguments"})
_DEFAULT_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/HKUDS/erza",
    "X-OpenRouter-Title": "Erza",
    "X-OpenRouter-Categories": "cli-agent,personal-agent",
}
_KIMI_THINKING_MODELS: frozenset[str] = frozenset(
    {
        "kimi-k2.5",
        "kimi-k2.6",
        "k2.6-code-preview",
    }
)
# Thinking-capable MiMo models per Xiaomi docs (see
# tests/providers/test_xiaomi_mimo_thinking.py). mimo-v2-flash is omitted
# because it does not support thinking.
_MIMO_THINKING_MODELS: frozenset[str] = frozenset(
    {
        "mimo-v2.5-pro",
        "mimo-v2.5",
        "mimo-v2-pro",
        "mimo-v2-omni",
    }
)
_OPENAI_COMPAT_REQUEST_TIMEOUT_S = 120.0

# Maps ProviderSpec.thinking_style → extra_body builder.
# Each builder takes a bool (thinking_enabled) and returns the dict to
# merge into extra_body, keeping the style→wire-format mapping in one place.
_THINKING_STYLE_MAP: dict[str, Any] = {
    "thinking_type": lambda on: {"thinking": {"type": "enabled" if on else "disabled"}},
    "enable_thinking": lambda on: {"enable_thinking": on},
    "reasoning_split": lambda on: {"reasoning_split": on},
}
_GATEWAY_REASONING_STYLE_MAP: dict[str, Any] = {
    "reasoning_effort": lambda effort: {"reasoning": {"effort": effort}},
}
_MODEL_THINKING_STYLES: dict[str, str] = {
    **dict.fromkeys(_KIMI_THINKING_MODELS, "thinking_type"),
    **dict.fromkeys(_MIMO_THINKING_MODELS, "thinking_type"),
}


def _model_slug(model_name: str) -> str:
    return model_name.lower().rsplit("/", 1)[-1]


def _model_thinking_style(model_name: str) -> str:
    return _MODEL_THINKING_STYLES.get(_model_slug(model_name), "")


def _thinking_styles_for(spec: ProviderSpec | None, model_name: str) -> list[str]:
    styles: list[str] = []
    if spec and spec.thinking_style:
        styles.append(spec.thinking_style)
    model_style = _model_thinking_style(model_name)
    if model_style and model_style not in styles:
        styles.append(model_style)
    return styles


def _thinking_extra_body(style: str, thinking_enabled: bool) -> dict[str, Any] | None:
    builder = _THINKING_STYLE_MAP.get(style)
    return builder(thinking_enabled) if builder else None


def _gateway_reasoning_extra_body(style: str, effort: str | None) -> dict[str, Any] | None:
    if not effort:
        return None
    builder = _GATEWAY_REASONING_STYLE_MAP.get(style)
    return builder(effort) if builder else None


def _openai_compat_timeout_s() -> float:
    """Return the bounded request timeout used for OpenAI-compatible providers."""
    return _float_env("ERZA_OPENAI_COMPAT_TIMEOUT_S", _OPENAI_COMPAT_REQUEST_TIMEOUT_S)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid {}={!r}; using {}", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive {}={!r}; using {}", name, raw, default)
        return default
    return value


def _short_tool_id() -> str:
    """9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Parse a tool-call ``arguments`` payload into a dict, defaulting to ``{}``.

    Providers occasionally emit arguments that repair to a bare string, number,
    or list.  The non-streaming path already guards with ``isinstance(..., dict)``;
    this helper gives the streaming accumulator the same guarantee so dispatch
    never receives a non-mapping payload.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    parsed = json_repair.loads(raw) if isinstance(raw, str) else raw
    return parsed if isinstance(parsed, dict) else {}


def _get(obj: Any, key: str) -> Any:
    """Get a value from dict or object attribute, returning None if absent."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _coerce_dict(value: Any) -> dict[str, Any] | None:
    """Try to coerce *value* to a dict; return None if not possible or empty."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value if value else None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict) and dumped:
            return dumped
    return None


def _extract_tc_extras(
    tc: Any,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Extract (extra_content, provider_specific_fields, fn_provider_specific_fields).

    Works for both SDK objects and dicts.  Captures Gemini ``extra_content``
    verbatim and any non-standard keys on the tool-call / function.
    """
    extra_content = _coerce_dict(_get(tc, "extra_content"))

    tc_dict = _coerce_dict(tc)
    prov = None
    fn_prov = None
    if tc_dict is not None:
        leftover = {
            k: v
            for k, v in tc_dict.items()
            if k not in _STANDARD_TC_KEYS and k != "extra_content" and v is not None
        }
        if leftover:
            prov = leftover
        fn = _coerce_dict(tc_dict.get("function"))
        if fn is not None:
            fn_leftover = {
                k: v for k, v in fn.items() if k not in _STANDARD_FN_KEYS and v is not None
            }
            if fn_leftover:
                fn_prov = fn_leftover
    else:
        prov = _coerce_dict(_get(tc, "provider_specific_fields"))
        fn_obj = _get(tc, "function")
        if fn_obj is not None:
            fn_prov = _coerce_dict(_get(fn_obj, "provider_specific_fields"))

    return extra_content, prov, fn_prov


def _uses_openrouter_attribution(spec: "ProviderSpec | None", api_base: str | None) -> bool:
    """Apply Erza attribution headers to OpenRouter requests by default."""
    if spec and spec.extra_headers:
        return True
    return bool(api_base and "openrouter" in api_base.lower())


_RESPONSES_FAILURE_THRESHOLD = 3
_RESPONSES_PROBE_INTERVAL_S = 300  # 5 minutes


def _is_local_endpoint(
    spec: "ProviderSpec | None",
    api_base: str | None,
) -> bool:
    """Return True when the endpoint is a local or LAN model server.

    Matches either the provider spec's ``is_local`` flag or common private-
    network patterns in the base URL (localhost, 127.x, 192.168.x, 10.x,
    172.16-31.x, Docker ``host.docker.internal``).
    """
    if spec and spec.is_local:
        return True
    if not api_base:
        return False
    raw = api_base.strip().lower()
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    try:
        host = parsed.hostname
    except ValueError:
        return False
    if host in {"localhost", "host.docker.internal"}:
        return True
    if not host:
        return False
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def _is_direct_openai_base(api_base: str | None) -> bool:
    """Return True for direct OpenAI endpoints, not generic OpenAI-compatible gateways."""
    if not api_base:
        return True
    normalized = api_base.strip().lower().rstrip("/")
    return "api.openai.com" in normalized and "openrouter" not in normalized


def _responses_circuit_key(
    model: str | None,
    default_model: str,
    reasoning_effort: str | None,
) -> str:
    model_name = (model or default_model).lower()
    effort = reasoning_effort.lower() if isinstance(reasoning_effort, str) else ""
    return f"{model_name}:{effort}"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict.

    Nested dicts are merged key-by-key; all other types in *override*
    replace the corresponding key in *base*.
    """
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_unique_list(base: Any, override: Any) -> Any:
    """Append list values while preserving order and removing duplicates."""
    if not isinstance(base, list) or not isinstance(override, list):
        return override
    result: list[Any] = []
    seen: set[str] = set()
    for value in [*base, *override]:
        try:
            key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except Exception:
            key = repr(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _merge_responses_extra_body(
    body: dict[str, Any],
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    """Merge configured Responses API body fields without clobbering tools."""
    reserved = {"include", "tools"}
    regular_extra = {key: value for key, value in extra_body.items() if key not in reserved}
    merged = _deep_merge(body, regular_extra)

    if "include" in extra_body:
        merged["include"] = _merge_unique_list(body.get("include"), extra_body["include"])

    if "tools" in extra_body:
        current_tools = body.get("tools")
        configured_tools = extra_body["tools"]
        if isinstance(current_tools, list) and isinstance(configured_tools, list):
            merged["tools"] = [*current_tools, *configured_tools]
        else:
            merged["tools"] = configured_tools

    return merged


# ---------------------------------------------------------------------------
# Response parsing (dict raw JSON and SDK Pydantic objects)
# ---------------------------------------------------------------------------


def _maybe_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    return None


def _extract_text_content(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            item_map = _maybe_mapping(item)
            if item_map:
                text = item_map.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            if isinstance(item, str):
                parts.append(item)
        return "".join(parts) or None
    return str(value)


def _extract_usage(response: Any) -> dict[str, int]:
    """Extract token usage from an OpenAI-compatible response.

    Handles both dict-based (raw JSON) and object-based (SDK Pydantic)
    responses.  Provider-specific ``cached_tokens`` fields are normalised
    under a single key; see the priority chain inside for details.
    """
    # --- resolve usage object ---
    usage_obj = None
    response_map = _maybe_mapping(response)
    if response_map is not None:
        usage_obj = response_map.get("usage")
    elif hasattr(response, "usage") and response.usage:
        usage_obj = response.usage

    usage_map = _maybe_mapping(usage_obj)
    if usage_map is not None:
        result = {
            "prompt_tokens": int(usage_map.get("prompt_tokens") or 0),
            "completion_tokens": int(usage_map.get("completion_tokens") or 0),
            "total_tokens": int(usage_map.get("total_tokens") or 0),
        }
    elif usage_obj:
        result = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
        }
    else:
        return {}

    # --- cached_tokens (normalised across providers) ---
    # Try nested paths first (dict), fall back to attribute (SDK object).
    # Priority order ensures the most specific field wins.
    for path in (
        ("prompt_tokens_details", "cached_tokens"),  # OpenAI/Zhipu/MiniMax/Qwen/Mistral/xAI
        ("cached_tokens",),  # StepFun/Moonshot (top-level)
        ("prompt_cache_hit_tokens",),  # DeepSeek/SiliconFlow
    ):
        cached = _get_nested_int(usage_map, path)
        if not cached and usage_obj:
            cached = _get_nested_int(usage_obj, path)
        if cached:
            result["cached_tokens"] = cached
            break

    return result


def _get_nested_int(obj: Any, path: tuple[str, ...]) -> int:
    """Drill into *obj* by *path* segments and return an ``int`` value.

    Supports both dict-key access and attribute access so it works
    uniformly with raw JSON dicts **and** SDK Pydantic models.
    """
    current = obj
    for segment in path:
        if current is None:
            return 0
        if isinstance(current, dict):
            current = current.get(segment)
        else:
            current = getattr(current, segment, None)
    return int(current or 0) if current is not None else 0


def _parse_response(response: Any, spec: Any) -> LLMResponse:
    if isinstance(response, str):
        return LLMResponse(content=response, finish_reason="stop")

    response_map = _maybe_mapping(response)
    if response_map is not None:
        choices = response_map.get("choices") or []
        if not choices:
            content = _extract_text_content(
                response_map.get("content") or response_map.get("output_text")
            )
            reasoning_content = _extract_text_content(response_map.get("reasoning_content"))
            if content is not None:
                return LLMResponse(
                    content=content,
                    reasoning_content=reasoning_content,
                    finish_reason=str(response_map.get("finish_reason") or "stop"),
                    usage=_extract_usage(response_map),
                )
            return LLMResponse(content="Error: API returned empty choices.", finish_reason="error")

        choice0 = _maybe_mapping(choices[0]) or {}
        msg0 = _maybe_mapping(choice0.get("message")) or {}
        content = _extract_text_content(msg0.get("content"))
        finish_reason = str(choice0.get("finish_reason") or "stop")

        raw_tool_calls: list[Any] = []
        # StepFun: fallback to reasoning field when content is empty
        if not content and msg0.get("reasoning") and spec and spec.reasoning_as_content:
            content = _extract_text_content(msg0.get("reasoning"))
        reasoning_content = msg0.get("reasoning_content")
        if not reasoning_content and msg0.get("reasoning"):
            reasoning_content = _extract_text_content(msg0.get("reasoning"))
        for ch in choices:
            ch_map = _maybe_mapping(ch) or {}
            m = _maybe_mapping(ch_map.get("message")) or {}
            tool_calls = m.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                raw_tool_calls.extend(tool_calls)
                if ch_map.get("finish_reason") in ("tool_calls", "stop"):
                    finish_reason = str(ch_map["finish_reason"])
            if not content:
                content = _extract_text_content(m.get("content"))
            if not reasoning_content:
                reasoning_content = m.get("reasoning_content")

        parsed_tool_calls = []
        for tc in raw_tool_calls:
            tc_map = _maybe_mapping(tc) or {}
            fn = _maybe_mapping(tc_map.get("function")) or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                args = json_repair.loads(args)
            ec, prov, fn_prov = _extract_tc_extras(tc)
            parsed_tool_calls.append(
                ToolCallRequest(
                    id=str(tc_map.get("id") or _short_tool_id()),
                    name=str(fn.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                    extra_content=ec,
                    provider_specific_fields=prov,
                    function_provider_specific_fields=fn_prov,
                )
            )

        return LLMResponse(
            content=content,
            tool_calls=parsed_tool_calls,
            finish_reason=finish_reason,
            usage=_extract_usage(response_map),
            reasoning_content=reasoning_content if isinstance(reasoning_content, str) else None,
        )

    if not response.choices:
        return LLMResponse(content="Error: API returned empty choices.", finish_reason="error")

    choice = response.choices[0]
    msg = choice.message
    content = msg.content
    finish_reason = choice.finish_reason

    raw_tool_calls: list[Any] = []
    for ch in response.choices:
        m = ch.message
        if hasattr(m, "tool_calls") and m.tool_calls:
            raw_tool_calls.extend(m.tool_calls)
            if ch.finish_reason in ("tool_calls", "stop"):
                finish_reason = ch.finish_reason
        if not content and m.content:
            content = m.content
        if not content and getattr(m, "reasoning", None) and spec and spec.reasoning_as_content:
            content = m.reasoning

    tool_calls = []
    for tc in raw_tool_calls:
        args = tc.function.arguments
        if isinstance(args, str):
            args = json_repair.loads(args)
        ec, prov, fn_prov = _extract_tc_extras(tc)
        tool_calls.append(
            ToolCallRequest(
                id=str(getattr(tc, "id", None) or _short_tool_id()),
                name=tc.function.name,
                arguments=args,
                extra_content=ec,
                provider_specific_fields=prov,
                function_provider_specific_fields=fn_prov,
            )
        )

    reasoning_content = getattr(msg, "reasoning_content", None) or None
    if not reasoning_content and getattr(msg, "reasoning", None):
        reasoning_content = msg.reasoning

    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason or "stop",
        usage=_extract_usage(response),
        reasoning_content=reasoning_content,
    )


def _parse_stream_chunks(chunks: list[Any]) -> LLMResponse:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tc_bufs: dict[int, dict[str, Any]] = {}
    finish_reason = "stop"
    usage: dict[str, int] = {}

    def _accum_tc(tc: Any, idx_hint: int) -> None:
        """Accumulate one streaming tool-call delta into *tc_bufs*."""
        tc_index: int = _get(tc, "index") if _get(tc, "index") is not None else idx_hint
        buf = tc_bufs.setdefault(
            tc_index,
            {
                "id": "",
                "name": "",
                "arguments": "",
                "extra_content": None,
                "prov": None,
                "fn_prov": None,
            },
        )
        tc_id = _get(tc, "id")
        if tc_id:
            buf["id"] = str(tc_id)
        fn = _get(tc, "function")
        if fn is not None:
            fn_name = _get(fn, "name")
            if fn_name:
                buf["name"] = str(fn_name)
            fn_args = _get(fn, "arguments")
            if fn_args:
                buf["arguments"] += str(fn_args)
        ec, prov, fn_prov = _extract_tc_extras(tc)
        if ec:
            buf["extra_content"] = ec
        if prov:
            buf["prov"] = prov
        if fn_prov:
            buf["fn_prov"] = fn_prov

    def _accum_legacy_function_call(function_call: Any) -> None:
        """Accumulate legacy ``delta.function_call`` streaming chunks."""
        if not function_call:
            return
        buf = tc_bufs.setdefault(
            0,
            {
                "id": "",
                "name": "",
                "arguments": "",
                "extra_content": None,
                "prov": None,
                "fn_prov": None,
            },
        )
        fn_name = _get(function_call, "name")
        if fn_name:
            buf["name"] = str(fn_name)
        fn_args = _get(function_call, "arguments")
        if fn_args:
            buf["arguments"] += str(fn_args)

    for chunk in chunks:
        if isinstance(chunk, str):
            content_parts.append(chunk)
            continue

        chunk_map = _maybe_mapping(chunk)
        if chunk_map is not None:
            choices = chunk_map.get("choices") or []
            if not choices:
                usage = _extract_usage(chunk_map) or usage
                text = _extract_text_content(
                    chunk_map.get("content") or chunk_map.get("output_text")
                )
                if text:
                    content_parts.append(text)
                continue
            choice = _maybe_mapping(choices[0]) or {}
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
            delta = _maybe_mapping(choice.get("delta")) or {}
            text = _extract_text_content(delta.get("content"))
            if text:
                content_parts.append(text)
            text = _extract_text_content(delta.get("reasoning_content"))
            if not text:
                text = _extract_text_content(delta.get("reasoning"))
            if text:
                reasoning_parts.append(text)
            for idx, tc in enumerate(delta.get("tool_calls") or []):
                _accum_tc(tc, idx)
            _accum_legacy_function_call(delta.get("function_call"))
            usage = _extract_usage(chunk_map) or usage
            continue

        if not chunk.choices:
            usage = _extract_usage(chunk) or usage
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        delta = choice.delta
        if delta and delta.content:
            content_parts.append(delta.content)
        if delta:
            reasoning = getattr(delta, "reasoning_content", None)
            if not reasoning:
                reasoning = getattr(delta, "reasoning", None)
            if reasoning:
                reasoning_parts.append(reasoning)
        for tc in (getattr(delta, "tool_calls", None) or []) if delta else []:
            _accum_tc(tc, getattr(tc, "index", 0))
        if delta:
            _accum_legacy_function_call(getattr(delta, "function_call", None))

    # Some providers (e.g. Zhipu/GLM) reuse the same tool_call id for
    # parallel tool calls in streaming mode. Deduplicate before building
    # the response so downstream tool messages don't collide.
    _seen_tc_ids: set[str] = set()
    for b in tc_bufs.values():
        if not b["id"] or b["id"] in _seen_tc_ids:
            b["id"] = _short_tool_id()
        _seen_tc_ids.add(b["id"])

    return LLMResponse(
        content="".join(content_parts) or None,
        tool_calls=[
            ToolCallRequest(
                id=b["id"] or _short_tool_id(),
                name=b["name"],
                # Mirror the non-streaming ``_parse`` guard: a payload that
                # repairs to a non-dict (bare string / number / list) must
                # not leak into downstream tool dispatch as a weird type.
                arguments=_parse_tool_arguments(b["arguments"]),
                extra_content=b.get("extra_content"),
                provider_specific_fields=b.get("prov"),
                function_provider_specific_fields=b.get("fn_prov"),
            )
            for b in tc_bufs.values()
        ],
        finish_reason=finish_reason,
        usage=usage,
        reasoning_content="".join(reasoning_parts) or None,
    )
