"""WebSocket channel HTTP dispatch & reload/notify mixin.

Splits the HTTP dispatch, the route-deps builder, the cron/MCP reload and
session-notify callbacks, the WS-upgrade gate and the API-token validation
out of ``channel.py``.  MCP hot-reload is delegated to the constructor-
injected ``mcp_reloader`` callback (the composition root wires the tools
implementation via ``WebUIContext``); ``get_media_dir`` remains a patchable
``channel`` module global so test monkeypatches work.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger
from websockets.http11 import Request as WsRequest

from erza.channels.websocket import channel as _ch

from ._http_router import RouteDeps, router
from ._http_routes import (
    _bearer_token,
    _http_error,
    _http_json_response,
    _parse_query,
    _parse_request_path,
    _query_first,
)
from ._ws_upgrade import _is_websocket_upgrade, _normalize_config_path
from .api._settings_store import SettingsStore


class HttpMixin:
    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to a handler or to the WS upgrade path.

        分派顺序:
        1. ``token_issue_path`` (可选的自定义令牌签发端点,保留在 channel)。
        2. ``/webui/bootstrap`` (token bootstrap,需要 channel 状态:Origin 校验 +
           限流 + 双 token 池写入,保留在 channel)。
        3. 声明式路由层(60 个精确 + 5 个正则,handler 已迁移到 ``handlers/``)。
        4. WebSocket 升级(只允许在配置路径上的真 WS 握手)。
        5. API 404(避免给 API 客户端吐 SPA HTML,导致前端 JSON 解析炸掉)。
        6. 静态文件(SPA fallback 到 index.html)。
        """
        got, query = _parse_request_path(request.path)

        # 1. 自定义 token 签发端点
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue_http(connection, request)

        # 1b. /health (公开 liveness probe)
        # 设计 §4.6: Compose healthcheck 必须改用公开的 /health,避免 API 启用
        # key 后 /v1/models 返回 401,导致 healthcheck 永远判定容器不健康。
        # /health 不需要 token、不读任何状态,只表示进程能接收 HTTP 请求。
        if got == "/health":
            return _http_json_response({"status": "ok"})

        # 2. /webui/bootstrap (channel-side stateful bootstrap endpoint)
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # 3. 声明式路由层:handler 不再持有 self,改由 RouteDeps 注入依赖
        deps = self._build_route_deps()
        api_response = await router.dispatch(deps, connection, request)
        if api_response is not None:
            return api_response

        # 4. WebSocket 升级
        ws_matched, ws_response = self._dispatch_websocket_upgrade(connection, request, got, query)
        if ws_matched:
            return ws_response

        # API clients should never receive the SPA shell for an unknown route.
        # Returning HTML here makes the WebUI fail with "Unexpected token <"
        # when a dev server is pointed at an older gateway.
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # 5. 静态文件
        if self._static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    def _build_route_deps(self, settings: SettingsStore | None = None) -> RouteDeps:
        """构造一次请求所需的依赖快照,把 channel 实例状态显式注入到 handler。

        - 每次请求都构造新快照,handler 读到的总是最新状态(例如运行时
          ``runtime_capabilities``、``session_manager`` 等)。
        - 副作用回调(``with_restart_state``/``refresh_agent_model``/``reload_cron``/
          ``reload_mcp``/``notify_session_updated``/``invalidate_bootstrap_cache``)
          通过 callable 注入,handler 不再反向引用 channel,实现解耦。
        - ``reload_mcp`` 委托构造注入的 ``self._mcp_reloader`` 完成热重载(组合根
          经 ``WebUIContext`` 传入 tools 的热重载实现 ``request_mcp_reload``)。
        - ``mcp_connector`` 同样经 ``self._mcp_connector`` 传给 preset 测试(组合根
          经 ``WebUIContext`` 传入 tools 的 preset 连接实现)。
        - ``settings`` 默认新建无状态 ``SettingsStore``;测试需要注入计数/假
          store 时可直接传参覆盖。
        """
        return RouteDeps(
            workspace_path=self._workspace_path,
            webui_workspaces=self._webui_workspaces,
            session_manager=self._session_manager,
            cron_service=self._cron_service,
            tool_registry=self._tool_registry,
            provider_loader=self._provider_loader,
            runtime_model_name=self._runtime_model_name,
            runtime_surface=self._runtime_surface,
            runtime_capabilities=self._runtime_capabilities,
            media_secret=self._media_secret,
            bus=self.bus,
            logger=self.logger,
            check_api_token=self._check_api_token,
            is_localhost_connection=self._is_localhost_connection,
            is_origin_allowed=self._is_origin_allowed,
            with_restart_state=self._with_settings_restart_state,
            refresh_agent_model=self._maybe_refresh_agent_model,
            reload_cron=self._reload_cron_safe,
            reload_mcp=self._reload_mcp_safe,
            mcp_connector=self._mcp_connector,
            notify_session_updated=self._notify_session_updated_safe,
            invalidate_bootstrap_cache=self._invalidate_bootstrap_cache,
            sign_media_path=self._sign_media_path,
            sign_or_stage_media_path=self._sign_or_stage_media_path,
            get_media_dir=_ch.get_media_dir,
            settings=settings or SettingsStore(),
        )

    def _reload_cron_safe(self) -> None:
        """重新注册心跳/dream 系统 cron 任务,使新间隔立即生效(best-effort)。"""
        if self._cron_reloader is not None:
            try:
                self._cron_reloader()
            except Exception:
                logger.exception("Cron reloader failed after runtime settings update")

    async def _reload_mcp_safe(self) -> dict[str, Any]:
        """触发 MCP 服务热重载,经构造注入的 ``self._mcp_reloader`` 回调完成
        (组合根从 tools 的热重载实现 ``request_mcp_reload`` 传入)。回调为 None 时
        降级返回 requires_restart:生产路径组合根永远注入,仅未注入的测试构造
        走此降级。

        必须 ``await``:回调是协程,旧实现直接调用它只会产生一个从未被调度的
        协程对象,热重载实际不会发生。返回值供 ``McpReload`` 契约
        (``Callable[[], Awaitable[dict]]``)使用。
        """
        if self._mcp_reloader is None:
            return {"ok": False, "requires_restart": True}
        try:
            return await self._mcp_reloader(self.bus)
        except Exception:
            logger.exception("MCP reload failed after preset change")
            return {"ok": False, "requires_restart": True}

    def _notify_session_updated_safe(self, chat_id: str) -> None:
        """fire-and-forget: 通知连接的 WS 客户端刷新会话视图。"""
        try:
            task = asyncio.create_task(self.send_session_updated(chat_id))
            # 持引用防止任务被 GC 中途丢弃 (与 ChannelManager 后台任务同模式)。
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)
        except RuntimeError:
            # No running loop — the client will refresh on next poll/reconnect.
            pass

    def _invalidate_bootstrap_cache(self, name: str) -> None:
        """Best-effort invalidation of ContextBuilder's bootstrap file cache."""
        try:
            agent = getattr(self, "_agent_loop", None) or getattr(self, "agent_loop", None)
            ctx = getattr(agent, "context", None) if agent else None
            cache = getattr(ctx, "_bootstrap_cache", None) if ctx else None
            if cache is None:
                return
            target = self._workspace_path / name
            for key in list(cache):
                try:
                    if str(key) == str(target):
                        cache.pop(key, None)
                except Exception:
                    continue
        except Exception:
            # Cache invalidation is best-effort; mtime check covers it anyway.
            pass

    def _dispatch_websocket_upgrade(
        self,
        connection: Any,
        request: WsRequest,
        got: str,
        query: dict[str, list[str]],
    ) -> tuple[bool, Any | None]:
        """Authorize only real WS upgrade requests for the configured path."""
        expected_ws = self._expected_path()
        if got != expected_ws or not _is_websocket_upgrade(request):
            return False, None
        client_id = _query_first(query, "client_id") or ""
        if len(client_id) > 128:
            client_id = client_id[:128]
        if not self.is_allowed(client_id):
            return True, connection.respond(403, "Forbidden")
        return True, self._authorize_websocket_handshake(connection, request, query)

    def _check_api_token(self, request: WsRequest) -> bool:
        """Validate a request against the API token pool (multi-use, TTL-bound)."""
        self._purge_expired_api_tokens()
        # 安全提示:推荐客户端使用 ``Authorization: Bearer <token>`` 头传递 token,
        # 避免出现在 URL/Referer/日志中。``?token=`` 查询参数仅作为旧客户端的
        # 向后兼容回退保留,且仅在 Authorization 头缺失时才被读取(短路求值)。
        # 注意:不要在此方法或调用方记录完整 request.path,以免 token 泄漏到日志。
        token = _bearer_token(request.headers)
        if not token and getattr(self.config, "allow_query_token", True):
            # 仅当 Authorization 头缺失且 allow_query_token 开启时回退到 ?token=
            # 查询参数(向后兼容); 生产环境建议配置 allow_query_token=false 彻底禁用。
            token = _query_first(_parse_query(request.path), "token")
            if token:
                # 废弃警告:?token= 会出现在 URL 中,可能被日志/Referer/浏览器历史记录泄漏。
                # 建议客户端迁移到 Authorization: Bearer <token> 头。每次回退使用时只警告一次
                # 太吵,这里每次都记录(warning 级别),便于监控存量客户端迁移进度。
                self.logger.warning(
                    "使用 ?token= 查询参数鉴权已废弃,请改用 Authorization 头;token 可能泄漏到日志/Referer/浏览器历史"
                )
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._api_tokens.items()):
            if now > expiry:
                self._api_tokens.pop(token_key, None)
