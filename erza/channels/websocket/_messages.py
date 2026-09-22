"""WebSocket channel message / media / sessions / static / handshake mixin.

Splits the inbound-message handling, the media signing/staging helpers, the
session-list and webui-thread backward-compat wrappers, the static SPA
serving and the WS handshake authorization out of ``channel.py``.
Patchable names (``get_media_dir``) resolve at call time through the
``channel`` module namespace so test monkeypatches keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from erza.channels.websocket import channel as _ch
from erza.channels.websocket.api.media_api import sign_media_path, sign_or_stage_media_path
from erza.channels.websocket.api.transcript import rewrite_local_markdown_images
from erza.security.tokens import constant_time_equals

from ._http_router import RouteContext, router
from ._http_routes import _http_error, _parse_request_path, _query_first
from .static.serve import serve_static


class MessageMixin:
    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        meta = metadata or {}
        if meta.get("webui"):
            user_obj: dict[str, Any] = {
                "event": "user",
                "chat_id": chat_id,
                "text": content,
            }
            if media:
                user_obj["media_paths"] = list(media)
            mcp_presets = meta.get("mcp_presets")
            if isinstance(mcp_presets, list) and mcp_presets:
                user_obj["mcp_presets"] = mcp_presets
            self._try_append_webui_transcript(chat_id, user_obj)
        await super()._handle_message(
            sender_id,
            chat_id,
            content,
            media,
            metadata,
            session_key,
        )

    def _sign_media_path(self, abs_path: Path) -> str | None:
        """Return a ``/api/media/<sig>/<payload>`` URL for *abs_path*, or
        ``None`` when the path does not resolve inside the media root.

        The URL is self-authenticating: the signature binds the payload to
        this process's ``_media_secret``, so only paths we chose to sign can
        be fetched. The returned path is relative to the server origin; the
        client joins it against this server's HTTP origin (same host as WS).
        """
        return sign_media_path(
            abs_path,
            secret=self._media_secret,
            media_dir=lambda channel=None: _ch.get_media_dir(channel),
        )

    def _sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        """Return a signed media URL payload for *path*.

        Persisted inbound media already lives under ``get_media_dir`` and can
        be signed directly. Outbound bot-generated files may live anywhere on
        disk; copy those into the websocket media bucket first so the browser
        can fetch them through the existing signed media route without
        exposing arbitrary filesystem paths.
        """
        return sign_or_stage_media_path(
            path,
            secret=self._media_secret,
            media_dir=lambda channel=None: _ch.get_media_dir(channel),
            logger=self.logger,
        )

    def _rewrite_local_markdown_images(self, text: str) -> str:
        return rewrite_local_markdown_images(
            text,
            workspace_path=self._workspace_path,
            sign_path=self._sign_or_stage_media_path,
        )

    # -- Backward-compat wrappers for tests that call handlers directly -----
    # These thin wrappers delegate to the declarative router so tests written
    # against the old ``channel._handle_sessions_list(req)`` API keep working
    # without duplicating the handler logic.

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        """Backward-compat: delegates to the ``/api/sessions`` route handler."""
        deps = self._build_route_deps()
        got, query = _parse_request_path(request.path)
        entry = router._exact.get(got)
        if entry is None:
            return _http_error(404, "not found")
        ctx = RouteContext(deps=deps, connection=None, request=request, query=query, got=got)
        return entry.fn(ctx)  # type: ignore[return-value]

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        """Backward-compat: delegates to the ``/api/sessions/<key>/webui-thread`` handler."""
        deps = self._build_route_deps()
        # ``key`` is already URL-encoded by the caller (matches old signature).
        got = f"/api/sessions/{key}/webui-thread"
        _, query = _parse_request_path(request.path)
        for pattern, entry in router._regex:
            m = pattern.match(got)
            if m is not None:
                ctx = RouteContext(
                    deps=deps,
                    connection=None,
                    request=request,
                    query=query,
                    got=got,
                    path_vars=m.groupdict(),
                )
                return entry.fn(ctx)  # type: ignore[return-value]
        return _http_error(404, "not found")

    # -- Static files and WebSocket handshake ------------------------------

    def _serve_static(self, request_path: str) -> Response | None:
        """Resolve *request_path* against the built SPA directory; SPA fallback to index.html."""
        assert self._static_dist_path is not None
        return serve_static(self._static_dist_path, request_path)

    def _authorize_websocket_handshake(
        self, connection: Any, request: WsRequest, query: dict[str, list[str]]
    ) -> Any:
        # 浏览器 Origin 校验:空 Origin(非浏览器客户端)放行以保持向后兼容;
        # 非空 Origin 必须在白名单内,否则拒绝握手。这能阻止恶意网页通过
        # ``new WebSocket("ws://127.0.0.1:8765?token=...")`` 进行 CSWSH 攻击。
        if not self._is_origin_allowed(request):
            return connection.respond(403, "Forbidden")
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and constant_time_equals(supplied, static_token):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._take_issued_token_if_valid(supplied)
        return None
