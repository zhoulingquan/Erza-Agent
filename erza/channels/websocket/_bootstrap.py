"""WebSocket channel bootstrap & runtime-settings mixin.

Splits the bootstrap token endpoint, the absolute WS URL builder, the
restart-state decoration and the agent-model / webui-transcript helpers out
of ``channel.py``.  ``_resolve_bootstrap_model_name`` is resolved at call
time through the ``channel`` module namespace so the test monkeypatch of
``channel._default_model_name_from_config`` keeps working.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from loguru import logger
from websockets.http11 import Response

from erza import __version__
from erza.channels.websocket import channel as _ch
from erza.channels.websocket.api.settings_api import decorate_settings_payload
from erza.channels.websocket.api.transcript import append_transcript_object

from ._http_routes import (
    _http_error,
    _http_json_response,
    _http_response,
    _issue_route_secret_matches,
)
from ._ws_upgrade import _case_insensitive_header, _host_for_url, _safe_host_header


class BootstrapMixin:
    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        # 浏览器 Origin 校验:阻止恶意网页跨域 fetch() 获取 token。
        # 空 Origin(非浏览器客户端如 curl)放行,后续 secret/localhost 检查继续生效。
        # 这一层是问题 2 的关键防御:即便 secret 未配置 + localhost 连接,
        # 跨域浏览器请求仍会被拒绝,杜绝 CSRF 式 token 盗取。
        if not self._is_origin_allowed(request):
            return _http_error(403, "Forbidden")
        # When a secret is configured (token_issue_secret or static token),
        # validate it regardless of source IP.  This secures deployments
        # behind a reverse proxy where all connections appear as localhost.
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not self._is_localhost_connection(connection):
            # No secret configured: only allow localhost (local dev mode).
            return _http_error(403, "bootstrap is localhost-only")
        # Per-IP token issuance rate limit (60/min).
        if not self._check_rate_limit(self._token_rate_limiter, connection, "bootstrap"):
            return _http_json_response({"error": "rate limited"}, status=429)
        # Cap outstanding tokens to avoid runaway growth from a misbehaving client.
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        # Same string registered in both pools: the WS handshake consumes one copy
        # while the REST surface keeps validating the other until TTL expiry.
        self._issued_tokens[token] = expiry
        self._api_tokens[token] = expiry
        ws_url = self._bootstrap_ws_url(request)
        return _http_json_response(
            {
                "token": token,
                "ws_path": self._expected_path(),
                "ws_url": ws_url,
                "expires_in": self.config.token_ttl_s,
                "model_name": _ch._resolve_bootstrap_model_name(self._runtime_model_name),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._runtime_capabilities,
                "version": __version__,
                # 把 WebSocket 帧大小上限回传给前端,使 Composer 能在发送前
                # 校验附件 + 文本的总 UTF-8 字节数(设计 §4.5),超限直接拦截、
                # 保留草稿,避免服务端 1009 关闭连接后丢失输入。
                "max_message_bytes": int(self.config.max_message_bytes),
            }
        )

    def _bootstrap_ws_url(self, request: Any) -> str:
        """Absolute WS URL clients should prefer over a dev-server proxy."""
        headers = getattr(request, "headers", {}) or {}
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)

        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certfile.strip())
        scheme = "wss" if secure else "ws"
        return f"{scheme}://{host}{self._expected_path()}"

    def _with_settings_restart_state(
        self,
        payload: dict[str, Any],
        *,
        section: str | None = None,
    ) -> dict[str, Any]:
        """Keep restart-required state alive for this gateway process."""
        if section and payload.get("requires_restart"):
            self._settings_restart_sections.add(section)
        sections = sorted(self._settings_restart_sections)
        payload = dict(payload)
        if sections:
            payload["requires_restart"] = True
        return decorate_settings_payload(
            payload,
            surface=self._runtime_surface,
            runtime_capability_overrides=self._runtime_capabilities,
            restart_required_sections=sections,
        )

    def _maybe_refresh_agent_model(self) -> None:
        """Refresh the running agent's model after settings changes.

        Re-reads the on-disk config and, if the provider signature changed,
        swaps the active model and broadcasts runtime_model_updated.
        """
        if self._agent_model_refresher is not None:
            try:
                self._agent_model_refresher()
            except Exception:
                logger.exception("Agent model refresh failed after settings update")

    def _try_append_webui_transcript(self, chat_id: str, wire: dict[str, Any]) -> None:
        sk = f"websocket:{chat_id}"
        try:
            dup = json.loads(json.dumps(wire, ensure_ascii=False))
            append_transcript_object(sk, dup)
        except (ValueError, TypeError) as e:
            self.logger.warning("webui transcript append failed: {}", e)
