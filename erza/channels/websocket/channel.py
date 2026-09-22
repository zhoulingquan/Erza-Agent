"""WebSocket server channel: Erza acts as a WebSocket server and serves connected clients.

The module-level helpers (path/HTTP/MIME/session/config) live in sibling
``_``-prefixed modules and are re-imported here so the public surface
(``WebSocketChannel``, ``WebSocketConfig``, ``publish_runtime_model_update``)
and the test monkeypatch targets (``get_media_dir``,
``_default_model_name_from_config``) keep working unchanged.

MCP hot-reload and preset-connect are constructor-injected callbacks
(``mcp_reloader`` / ``mcp_connector``) wired by the composition root via
``WebUIContext`` — channels never import the MCP implementation directly.

The channel's instance surface is split across sibling ``_``-prefixed mixins
(``_tokens`` / ``_http`` / ``_bootstrap`` / ``_messages`` / ``_lifecycle`` /
``_envelope`` / ``_send``) whose methods compose into ``WebSocketChannel``
through MRO.  The patches that tests apply to this module's globals are
resolved at call time through ``channel``'s namespace by those mixins, so
monkeypatching keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from websockets.exceptions import ConnectionClosed

from erza.bus.queue import MessageBus
from erza.channels.base import BaseChannel
from erza.channels.websocket.api.settings_api import runtime_capabilities
from erza.channels.websocket.api.workspaces import WebUIWorkspaceController
from erza.config.paths import (
    get_media_dir,  # noqa: F401 — module attribute (tests patch channel globals)
    get_workspace_path,
)
from erza.session.goal_state import goal_state_ws_blob
from erza.session.webui_turns import websocket_turn_wall_started_at

from . import handlers  # noqa: F401 — side effect: registers declarative routes
from ._bootstrap import BootstrapMixin
from ._envelope import EnvelopeMixin
from ._http import HttpMixin

# Re-export helpers from sibling submodules so the public surface and the
# test monkeypatch targets keep working unchanged. The original module
# attribute path (``erza.channels.websocket.channel.<name>``) is
# what tests patch, so the binding must live in this module's globals.
from ._http_routes import (  # noqa: F401
    _API_KEY_RE,
    _MCP_PRESET_ACTIONS_BY_PATH,
    _MCP_VALUES_HEADER,
    _MCP_VALUES_HEADER_MAX_BYTES,
    _bearer_token,
    _collect_chunked_header,
    _decode_api_key,
    _http_error,
    _http_json_response,
    _http_response,
    _human_readable_size,
    _issue_route_secret_matches,
    _normalize_http_path,
    _parse_mcp_settings_query,
    _parse_query,
    _parse_request_path,
    _query_first,
)
from ._lifecycle import LifecycleMixin
from ._media_sign import (  # noqa: F401
    _DATA_URL_MIME_RE,
    _DOCUMENT_MIME_ALLOWED,
    _IMAGE_MIME_ALLOWED,
    _MAX_DOCUMENT_BYTES,
    _MAX_IMAGE_BYTES,
    _MAX_IMAGES_PER_MESSAGE,
    _MAX_VIDEO_BYTES,
    _MAX_VIDEOS_PER_MESSAGE,
    _UPLOAD_MIME_ALLOWED,
    _VIDEO_MIME_ALLOWED,
    _extract_data_url_mime,
)
from ._messages import MessageMixin
from ._send import SendMixin
from ._session import (  # noqa: F401
    _CHAT_ID_RE,
    _default_model_name_from_config,
    _is_valid_chat_id,
    _parse_envelope,
    _parse_inbound_payload,
    publish_runtime_model_update,
)
from ._tokens import TokenMixin
from ._ws_upgrade import (  # noqa: F401
    _LOCALHOSTS,
    WebSocketConfig,
    _case_insensitive_header,
    _host_for_url,
    _is_localhost,
    _is_websocket_upgrade,
    _normalize_config_path,
    _RateLimiter,
    _safe_host_header,
    _strip_trailing_slash,
)

if TYPE_CHECKING:
    from erza.session.manager import SessionManager


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str | None:
    """Prefer an in-process resolver (e.g. AgentLoop); else config-derived default."""
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config()


class WebSocketChannel(
    HttpMixin,
    TokenMixin,
    BootstrapMixin,
    MessageMixin,
    LifecycleMixin,
    EnvelopeMixin,
    SendMixin,
    BaseChannel,
):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        workspace_path: Path | None = None,
        restrict_to_workspace: bool = False,
        runtime_model_name: Callable[[], str | None] | None = None,
        runtime_surface: str = "browser",
        runtime_capabilities_overrides: dict[str, Any] | None = None,
        provider_loader: Callable[[], Any] | None = None,
        cron_reloader: Callable[[], None] | None = None,
        agent_model_refresher: Callable[[], None] | None = None,
        cron_service: Any = None,
        tool_registry: Any = None,
        mcp_reloader: Callable[[MessageBus], Awaitable[dict[str, Any]]] | None = None,
        mcp_connector: Callable[[dict, Any], Awaitable[dict]] | None = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        # Single-use tokens consumed at WebSocket handshake.
        self._issued_tokens: dict[str, float] = {}
        # Multi-use tokens for HTTP routes served beside WS; checked but not consumed.
        self._api_tokens: dict[str, float] = {}
        # 后台通知任务引用集: create_task 必须持引用, 否则任务可能在完成前被
        # 事件循环 GC 丢弃; done_callback 自动回收引用, stop 时统一取消。
        self._notify_tasks: set[asyncio.Task] = set()
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._session_manager = session_manager
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        self._workspace_path = (
            Path(workspace_path).expanduser()
            if workspace_path is not None
            else get_workspace_path()
        ).resolve(strict=False)
        self._default_restrict_to_workspace = restrict_to_workspace
        self._webui_workspaces = WebUIWorkspaceController(
            session_manager=self._session_manager,
            default_workspace=self._workspace_path,
            default_restrict_to_workspace=self._default_restrict_to_workspace,
        )
        self._runtime_model_name = runtime_model_name
        # Lazy provider accessor used by HTTP routes that need to call the
        # LLM (e.g. /api/agents/generate). Returns an LLMProvider or None.
        self._provider_loader = provider_loader
        self._cron_reloader = cron_reloader
        self._agent_model_refresher = agent_model_refresher
        self._cron_service = cron_service
        self._tool_registry = tool_registry
        self._mcp_reloader = mcp_reloader
        self._mcp_connector = mcp_connector
        self._runtime_surface = "native" if runtime_surface in {"native", "desktop"} else "browser"
        self._runtime_capabilities = runtime_capabilities(
            self._runtime_surface,
            runtime_capabilities_overrides,
        )
        self._settings_restart_sections: set[str] = set()
        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}
        # 每个 stream_text_buffer 最近一次追加 delta 的时间(单调时钟),
        # 供 TTL 清理使用,防止异常中断的流导致 buffer 永驻。
        self._stream_text_buffer_times: dict[tuple[str, str], float] = {}
        # 流缓冲 TTL 清理任务:周期性删除超过 _STREAM_TEXT_BUF_TTL 未更新的 buffer,
        # 同时清理 RateLimiter 中的陈旧 key,防止内存无限增长。
        self._periodic_cleanup_task: asyncio.Task | None = None
        # Process-local secret used to HMAC-sign media URLs. The signed URL is
        # the capability — anyone who holds a valid URL can fetch that one
        # file, nothing else. The secret regenerates on restart so links
        # become self-expiring (callers just refresh the session list).
        self._media_secret: bytes = secrets.token_bytes(32)
        # IP-level rate limiters (sliding window, per client IP).
        self._conn_rate_limiter = _RateLimiter(max_count=60, window_s=60.0)
        self._token_rate_limiter = _RateLimiter(max_count=60, window_s=60.0)
        self._media_rate_limiter = _RateLimiter(max_count=10, window_s=3600.0)
        # 浏览器 Origin 白名单:默认放行本地 WebUI 同源请求;若配置 allow_origin 则扩展。
        # 空 Origin(非浏览器客户端,如 curl)在 _is_origin_allowed 中单独放行,不在此集合中。
        self._origin_allowlist: set[str] = self._build_origin_allowlist()

    # -- Subscription bookkeeping -------------------------------------------

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """Replay an active sustained goal from session metadata after *chat_id* is subscribed.

        Goal metadata lives on the session JSONL and survives gateway restarts, but
        connected clients normally see it via ``goal_state`` / ``turn_end`` frames.
        Pushing here makes refresh + reconnect restore the strip without a new model turn.
        """
        if self._session_manager is None:
            return
        row = self._session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip state after subscribe (same-process refresh)."""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)
