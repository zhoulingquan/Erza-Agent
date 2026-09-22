"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection.

The channel surface was split into mixins (``_mentions`` / ``_reactions`` /
``_stream_maintenance`` / ``_cards`` / ``_media_xfer`` / ``_sender`` / ``_send`` /
``_events``) while this module stays the real ``erza.channels.feishu.channel``:
it keeps the module-level constants, the config model, the identity helpers and
the ``FeishuChannel`` composition plus its login / WebSocket lifecycle / state
core.  Patchable names (``get_media_dir``) and package-reexport names
(``_extract_post_content`` / ``_FeishuStreamBuf``) remain module attributes so
tests and ``__init__.py`` keep working unchanged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Callable, Literal

from pydantic import Field
from rich.markup import escape

from erza.bus.queue import MessageBus
from erza.channels.base import BaseChannel
from erza.channels.feishu._cards import CardMixin
from erza.channels.feishu._content_extract import (
    _extract_post_content,  # noqa: F401  — re-export (package __init__)
)
from erza.channels.feishu._events import EventMixin
from erza.channels.feishu._feishu_instances import DEFAULT_INSTANCE_ID
from erza.channels.feishu._feishu_ws import get_feishu_ws_runner
from erza.channels.feishu._media_xfer import MediaXferMixin
from erza.channels.feishu._mentions import MentionMixin
from erza.channels.feishu._reactions import ReactionMixin
from erza.channels.feishu._registration import (
    _LOGIN_CONSOLE,
    _feishu_app_identity_key,
    qr_register,
    save_registration_result,
    sync_saved_feishu_identity_boundary,
)
from erza.channels.feishu._send import SendMixin
from erza.channels.feishu._sender import SendSyncMixin
from erza.channels.feishu._stream_buf import (
    _FeishuStreamBuf,  # noqa: F401  — re-export (package __init__)
)
from erza.channels.feishu._stream_maintenance import StreamMaintenanceMixin
from erza.config.paths import (
    get_media_dir,  # noqa: F401  — module attribute (tests patch channel globals)
)
from erza.config.schema import Base
from erza.utils.logging_bridge import redirect_lib_logging

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None


def _identity_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _feishu_section_and_saver() -> tuple[dict[str, Any], Callable[[dict[str, Any]], None]]:
    """Resolve the persisted ``channels.feishu`` section plus a section saver.

    The registration helpers receive only the feishu section (owned config)
    plus a callback, so the full config load / setattr / disk-save path lives
    here with the caller that already holds the full config.
    """
    from erza.channels.websocket.api._settings_store import SettingsStore

    settings = SettingsStore()
    config = settings.read()
    section = getattr(config.channels, "feishu", None)
    if not isinstance(section, dict):
        section = {}

    def save(next_section: dict[str, Any]) -> None:
        setattr(config.channels, "feishu", next_section)
        settings.write(config)

    return section, save


def _load_lark_runtime() -> tuple[Any, str, str]:
    """Import the heavy Feishu SDK lazily.

    lark_oapi imports a large generated API surface at module import time, so
    keep it out of channel discovery and constructor paths.

    .. warning::

        已知 anti-pattern（待后续重构）：``lark_oapi.ws.client`` 首次 import 时
        会在「当前线程」创建一个 asyncio event loop 并存到模块级 ``loop`` 属性。
        本函数通常通过 ``asyncio.to_thread`` 在线程池的 worker 线程中执行，因此
        这个临时 loop 会绑定到可复用的 worker 线程上，可能影响同进程其他频道
        后续在该线程上调用 ``asyncio.get_event_loop()`` 的行为。

        为降低污染，首次导入完成时做最小化清理：
        1. 关闭 lark 创建的临时 loop（仅在它未运行、未关闭时）；
        2. 将 ``lark_ws_client.loop`` 重置为 None，避免后续 ws runner 误用；
        3. 当当前线程的 loop 非 running 时，调用 ``asyncio.set_event_loop(None)``
           解除绑定。

        清理仅在与 lark 导入同一次执行时进行（``ws_client_already_imported``
        为 False），且仅在 worker 线程中进行（不在主线程），避免影响主线程的
        running loop 或重复覆盖其他模块已设置的 loop。后续若 lark SDK 修复了
        线程问题，应直接删除这段清理逻辑。
    """
    import sys

    ws_client_already_imported = "lark_oapi.ws.client" in sys.modules
    import lark_oapi as lark
    import lark_oapi.ws.client as lark_ws_client
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

    if not ws_client_already_imported and threading.current_thread() is not threading.main_thread():
        # 关闭 lark 在导入时为当前 worker 线程创建的临时 loop。
        import_loop = getattr(lark_ws_client, "loop", None)
        if import_loop is not None and not import_loop.is_running() and not import_loop.is_closed():
            import_loop.close()
        lark_ws_client.loop = None

        # 解除当前 worker 线程的 event loop 绑定，避免线程被线程池复用后
        # 影响其他频道。仅当 loop 非 running 时才清除（防御性兜底，避免误清
        # 掉其他模块在同一线程上设置的 running loop）。
        try:
            current_loop = asyncio.get_event_loop_policy().get_event_loop()
        except Exception:
            current_loop = None
        if current_loop is None or not current_loop.is_running():
            with suppress(Exception):
                asyncio.set_event_loop(None)

    return lark, FEISHU_DOMAIN, LARK_DOMAIN


def fetch_feishu_app_identity(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
) -> dict[str, str]:
    """Fetch the user-facing Feishu/Lark app identity for display.

    This is best-effort metadata for WebUI presentation.  Callers should treat
    an empty result as a normal fallback path.
    """
    if not FEISHU_AVAILABLE or not app_id or not app_secret:
        return {}

    try:
        lark, feishu_domain, lark_domain = _load_lark_runtime()
        from lark_oapi.api.application.v6.model.get_application_request import (
            GetApplicationRequest,
        )

        sdk_domain = lark_domain if domain == "lark" else feishu_domain
        client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(sdk_domain)
            .timeout(5)
            .build()
        )
        request = GetApplicationRequest.builder().app_id(app_id).lang("zh_cn").build()
        response = client.application.v6.application.get(request)
        if hasattr(response, "success") and not response.success():
            return {}

        app = getattr(getattr(response, "data", None), "app", None)
        if app is None:
            return {}

        identity: dict[str, str] = {}
        display_name = str(getattr(app, "app_name", "") or "").strip()
        avatar_url = str(getattr(app, "avatar_url", "") or "").strip()
        if display_name:
            identity["displayName"] = display_name
        if avatar_url:
            identity["avatarUrl"] = avatar_url
        if identity:
            identity["identityFetchedAt"] = _identity_timestamp()
        return identity
    except Exception:
        return {}


# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    instance_id: str = DEFAULT_INSTANCE_ID
    name: str = "erza"
    identity_key: str = ""
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    react_emoji: str = "THUMBSUP"
    done_emoji: str | None = None  # Emoji to show when task is completed (e.g., "DONE", "OK")
    tool_hint_prefix: str = "\U0001f527"  # Prefix for inline tool hints (default: 🔧)
    group_policy: Literal["open", "mention"] = "mention"
    reply_to_message: bool = False  # If True, bot replies quote the user's original message
    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"  # Set to "lark" for international Lark
    topic_isolation: bool = (
        True  # If True, each topic in group chat gets its own session (isolation)
    )


_STREAM_ELEMENT_ID = "streaming_md"
_NEW_SESSION_DIVIDER_CONTENT = json.dumps(
    {
        "type": "divider",
        "params": {"divider_text": {"text": "New session started."}},
    }
)


class FeishuChannel(
    MentionMixin,
    ReactionMixin,
    StreamMaintenanceMixin,
    CardMixin,
    MediaXferMixin,
    SendSyncMixin,
    SendMixin,
    EventMixin,
    BaseChannel,
):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"
    display_name = "Feishu"

    _STREAM_EDIT_INTERVAL = 0.5  # throttle between CardKit streaming updates
    # 流缓冲 TTL:超过该时长(秒)未更新的 buf 视为陈旧并被清理。
    # 30 分钟足以覆盖正常流式回复间隔,异常中断的流不会长期占用内存。
    _STREAM_BUF_TTL = 1800
    # 清理任务周期:每 5 分钟扫描一次,在及时回收与 CPU 开销之间取折中。
    _STREAM_BUF_CLEANUP_INTERVAL = 300

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return FeishuConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = FeishuConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_runner = get_feishu_ws_runner()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_bufs: dict[str, _FeishuStreamBuf] = {}
        self._bot_open_id: str | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id
        # 流缓冲 TTL 清理任务:周期性删除超过 _STREAM_BUF_TTL 未更新的 buf,
        # 防止异常中断的流(无 stream_end)导致 _stream_bufs 内存泄漏。
        self._stream_buf_cleanup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # QR login — writes credentials directly to config.json
    # ------------------------------------------------------------------

    async def login(self, force: bool = False) -> bool:
        """Perform QR code scan-to-create login for Feishu/Lark.

        Uses the Feishu device-code registration flow to create a new bot
        application automatically.  Opens a URL for the user to authorize
        with the Feishu or Lark mobile app.

        On success, writes ``appId``, ``appSecret``, and ``domain`` to
        ``channels.feishu`` in ``config.json`` and sets ``enabled: true``.

        Args:
            force: If True, clear existing credentials and force re-authentication.

        Returns True on success.
        """
        if force:
            self.config.app_id = ""
            self.config.app_secret = ""

        if self.config.app_id and self.config.app_secret:
            _LOGIN_CONSOLE.print("[green]Feishu/Lark is already authenticated.[/green]")
            _LOGIN_CONSOLE.print("Use --force to re-authenticate with a new bot.\n")
            return True

        _LOGIN_CONSOLE.print(
            "Authorize with the mobile app. erza will save the new bot credentials.\n"
        )

        result = qr_register(initial_domain=self.config.domain or "feishu")
        if not result:
            _LOGIN_CONSOLE.print(
                "[yellow]Login was not completed.[/yellow] "
                "Run 'erza channels login feishu --force' to retry."
            )
            return False

        self.config.app_id = result["app_id"]
        self.config.app_secret = result["app_secret"]
        self.config.domain = result.get("domain", "feishu")

        section, save_section = _feishu_section_and_saver()
        save_registration_result(
            result,
            instance_id=self.config.instance_id,
            name=self.config.name,
            feishu_section=section,
            save_feishu_section=save_section,
        )

        _LOGIN_CONSOLE.print("\n[green]Feishu/Lark login complete.[/green]")
        _LOGIN_CONSOLE.print(f"App ID: {escape(result['app_id'])}")
        _LOGIN_CONSOLE.print(f"Domain: {escape(self.config.domain)}")
        return True

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """Register an event handler only when the SDK supports it."""
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            self.logger.error("SDK not installed. Run: erza plugins enable feishu")
            return

        if not self.config.app_id or not self.config.app_secret:
            self.logger.error(
                "app_id and app_secret not configured. "
                "Run 'erza channels login feishu' to set up via QR code."
            )
            return

        section, save_section = _feishu_section_and_saver()
        if sync_saved_feishu_identity_boundary(
            instance_id=self.config.instance_id,
            app_id=self.config.app_id,
            domain=self.config.domain,
            feishu_section=section,
            save_feishu_section=save_section,
        ):
            self.config.identity_key = _feishu_app_identity_key(
                self.config.app_id, self.config.domain
            )
            self.config.allow_from = []
            self.logger.info(
                "Feishu app identity changed for {}; cleared paired users for this assistant",
                self.name,
            )

        lark, feishu_domain, lark_domain = await asyncio.to_thread(_load_lark_runtime)

        redirect_lib_logging("Lark")

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        domain = lark_domain if self.config.domain == "lark" else feishu_domain
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self._on_message_sync)
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_deleted_v1", self._on_reaction_deleted
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        # Silence "processor not found" errors when bots are added/removed from groups.
        # These events carry no actionable data for the agent.
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_added_v1",
            lambda _: None,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_deleted_v1",
            lambda _: None,
        )
        event_handler = builder.build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            domain=domain,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        await self._ws_runner.start_client(self.name, self._ws_client)

        # Fetch bot's own open_id for accurate @mention matching
        self._bot_open_id = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_bot_open_id
        )
        if self._bot_open_id:
            self.logger.info("bot open_id: {}", self._bot_open_id)
        else:
            self.logger.warning("Could not fetch bot open_id; @mention matching may be inaccurate")

        self.logger.info("bot started with WebSocket long connection")
        self.logger.info("No public IP required - using WebSocket to receive events")

        # 启动流缓冲 TTL 清理后台任务
        self._stream_buf_cleanup_task = asyncio.create_task(self._stream_buf_cleanup_loop())

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        Stop the Feishu bot.

        Notice: lark.ws.Client does not expose stop method， simply exiting the program will close the client.

        Reference: https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        # 取消流缓冲清理任务,等待其退出
        if self._stream_buf_cleanup_task is not None:
            self._stream_buf_cleanup_task.cancel()
            with suppress(Exception):
                await self._stream_buf_cleanup_task
            self._stream_buf_cleanup_task = None
        await self._ws_runner.stop_client(self.name)
        self.logger.info("bot stopped")

    def _fetch_bot_open_id(self) -> str | None:
        """Fetch the bot's own open_id via GET /open-apis/bot/v3/info."""
        try:
            import lark_oapi as lark

            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.APP})
                .build()
            )
            response = self._client.request(request)
            if response.success():
                import json

                data = json.loads(response.raw.content)
                bot = (data.get("data") or data).get("bot") or data.get("bot") or {}
                return bot.get("open_id")
            self.logger.warning(
                "Failed to get bot info: code={}, msg={}", response.code, response.msg
            )
            return None
        except Exception as e:
            self.logger.warning("Error fetching bot info: {}", e)
            return None
