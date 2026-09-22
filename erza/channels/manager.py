"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from erza.bus.events import OutboundMessage
from erza.bus.queue import MessageBus
from erza.channels._transcription import (
    resolve_transcription_base,
    resolve_transcription_key,
)
from erza.channels.base import BaseChannel
from erza.channels.webui_context import (
    _UNSET,
    WebUIContext,
    resolve_webui_context,
)
from erza.config.schema import Config
from erza.utils.restart import (
    consume_restart_notice_from_env,
    format_restart_completed_message,
)

if TYPE_CHECKING:
    from erza.session.manager import SessionManager


def _default_webui_dist() -> Path | None:
    """Return the absolute path to the bundled webui dist directory if it exists."""
    try:
        import erza.channels.websocket.static as web_pkg  # type: ignore[import-not-found]
    except ImportError:
        return None
    candidate = Path(web_pkg.__file__).resolve().parent / "dist"
    return candidate if candidate.is_dir() else None


# Retry delays for message sending (exponential backoff: 1s, 2s, 4s)
_SEND_RETRY_DELAYS = (1, 2, 4)

_BOOL_CAMEL_ALIASES: dict[str, str] = {
    "send_progress": "sendProgress",
    "send_tool_hints": "sendToolHints",
    "show_reasoning": "showReasoning",
}


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    # 去重指纹缓存上限:超过时按 LRU 策略淘汰最旧条目,避免长期运行导致内存无限增长。
    _MAX_ORIGIN_REPLY_FINGERPRINTS = 10000

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        context: WebUIContext | None = None,
        # 以下 webui_* 关键字参数为兼容透传(已废弃):显式传入时覆盖
        # context 对应字段并触发 DeprecationWarning,新增调用请改用 context。
        webui_runtime_model_name: Any = _UNSET,
        webui_static_dist: Any = _UNSET,
        webui_runtime_surface: Any = _UNSET,
        webui_runtime_capabilities: Any = _UNSET,
        webui_provider_loader: Any = _UNSET,
        webui_cron_reloader: Any = _UNSET,
        webui_agent_model_refresher: Any = _UNSET,
        webui_cron_service: Any = _UNSET,
        webui_tool_registry: Any = _UNSET,
        webui_mcp_reloader: Any = _UNSET,
        webui_mcp_connector: Any = _UNSET,
        # 以下四个参数为 transcription / websocket 装配的显式注入(组合根在
        # 持有全量 config 的组合侧取值后传入)。为 None 时回退读 config,
        # 保持旧构造 ``ChannelManager(config, bus)`` 在测试中可用。
        groq_api_key: str | None = None,
        groq_api_base: str | None = None,
        restrict_to_workspace: bool | None = None,
        workspace_path: str | Path | None = None,
    ):
        webui = resolve_webui_context(
            context,
            {
                "webui_runtime_model_name": webui_runtime_model_name,
                "webui_static_dist": webui_static_dist,
                "webui_runtime_surface": webui_runtime_surface,
                "webui_runtime_capabilities": webui_runtime_capabilities,
                "webui_provider_loader": webui_provider_loader,
                "webui_cron_reloader": webui_cron_reloader,
                "webui_agent_model_refresher": webui_agent_model_refresher,
                "webui_cron_service": webui_cron_service,
                "webui_tool_registry": webui_tool_registry,
                "webui_mcp_reloader": webui_mcp_reloader,
                "webui_mcp_connector": webui_mcp_connector,
            },
        )
        self.config = config
        self.bus = bus
        self._groq_api_key = groq_api_key
        self._groq_api_base = groq_api_base
        self._restrict_to_workspace = restrict_to_workspace
        self._workspace_path = workspace_path
        self._session_manager = session_manager
        self._webui_runtime_model_name = webui.runtime_model_name
        self._webui_static_dist = webui.static_dist
        self._webui_runtime_surface = webui.runtime_surface
        self._webui_runtime_capabilities = dict(webui.runtime_capabilities or {})
        self._webui_provider_loader = webui.provider_loader
        self._webui_cron_reloader = webui.cron_reloader
        self._webui_agent_model_refresher = webui.agent_model_refresher
        self._webui_cron_service = webui.cron_service
        self._webui_tool_registry = webui.tool_registry
        self._webui_mcp_reloader = webui.mcp_reloader
        self._webui_mcp_connector = webui.mcp_connector
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        # 后台发送任务引用集: create_task 必须持引用, 否则任务可能在完成前被
        # 事件循环 GC 丢弃; done_callback 自动回收引用, stop_all 时统一取消。
        self._background_tasks: set[asyncio.Task] = set()
        # 去重指纹缓存:使用 OrderedDict 实现 LRU,访问/写入时 move_to_end,
        # 超过 _MAX_ORIGIN_REPLY_FINGERPRINTS 时淘汰最旧条目。
        self._origin_reply_fingerprints: OrderedDict[tuple[str, str, str], str] = OrderedDict()

        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        from erza.channels.registry import (
            ChannelDependencyError,
            discover_channel_names,
            discover_enabled,
        )

        transcription_provider = self.config.channels.transcription_provider
        transcription_key = resolve_transcription_key(
            transcription_provider,
            getattr(self, "_groq_api_key", None)
            or self._resolve_transcription_config_value("groq", "api_key"),
        )
        transcription_base = resolve_transcription_base(
            transcription_provider,
            getattr(self, "_groq_api_base", None)
            or self._resolve_transcription_config_value("groq", "api_base"),
        )
        transcription_language = self.config.channels.transcription_language

        # Collect enabled module names first, then only import those.
        # Channel configs live in ChannelsConfig's extra fields (via
        # extra="allow"), so we enumerate candidates from pkgutil scan
        # (cheap, no imports) and any plugin keys in __pydantic_extra__.
        names = discover_channel_names()
        candidate_names = set(names)
        extra = getattr(self.config.channels, "__pydantic_extra__", None) or {}
        candidate_names.update(extra.keys())

        enabled_names: set[str] = set()
        for name in candidate_names:
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            if (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            ):
                enabled_names.add(name)

        # Strict discovery: an enabled channel whose optional dependencies
        # (extras) are missing surfaces as a loud startup error with the
        # matching `pip install` hint, instead of a silently skipped channel.
        try:
            discovered = discover_enabled(enabled_names, _names=names)
        except ChannelDependencyError as e:
            logger.error("{}", e)
            # Degrade gracefully: still load the remaining healthy channels
            # (websocket/WebUI included) so one missing extra doesn't take
            # the whole gateway down.
            discovered = discover_enabled(enabled_names, _names=names, strict=False)

        for name, cls in discovered.items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            try:
                kwargs: dict[str, Any] = {}
                if cls.name == "websocket":
                    if self._session_manager is not None:
                        kwargs["session_manager"] = self._session_manager
                        static_path = _default_webui_dist() if self._webui_static_dist else None
                        if static_path is not None:
                            kwargs["static_dist_path"] = static_path
                    kwargs["workspace_path"] = (
                        getattr(self, "_workspace_path", None) or self.config.workspace_path
                    )
                    restrict = getattr(self, "_restrict_to_workspace", None)
                    kwargs["restrict_to_workspace"] = (
                        restrict
                        if restrict is not None
                        else getattr(
                            getattr(self.config, "tools", None),
                            "restrict_to_workspace",
                            True,
                        )
                    )
                    if self._webui_runtime_model_name is not None:
                        kwargs["runtime_model_name"] = self._webui_runtime_model_name
                    if self._webui_provider_loader is not None:
                        kwargs["provider_loader"] = self._webui_provider_loader
                    kwargs["runtime_surface"] = self._webui_runtime_surface
                    kwargs["runtime_capabilities_overrides"] = self._webui_runtime_capabilities
                    if self._webui_cron_reloader is not None:
                        kwargs["cron_reloader"] = self._webui_cron_reloader
                    if self._webui_agent_model_refresher is not None:
                        kwargs["agent_model_refresher"] = self._webui_agent_model_refresher
                    if self._webui_cron_service is not None:
                        kwargs["cron_service"] = self._webui_cron_service
                    if self._webui_tool_registry is not None:
                        kwargs["tool_registry"] = self._webui_tool_registry
                    if self._webui_mcp_reloader is not None:
                        kwargs["mcp_reloader"] = self._webui_mcp_reloader
                    if self._webui_mcp_connector is not None:
                        kwargs["mcp_connector"] = self._webui_mcp_connector
                channel = cls(section, self.bus, **kwargs)
                channel.transcription_provider = transcription_provider
                channel.transcription_api_key = transcription_key
                channel.transcription_api_base = transcription_base
                channel.transcription_language = transcription_language
                channel.send_progress = self._resolve_bool_override(
                    section,
                    "send_progress",
                    self.config.channels.send_progress,
                )
                channel.send_tool_hints = self._resolve_bool_override(
                    section,
                    "send_tool_hints",
                    self.config.channels.send_tool_hints,
                )
                channel.show_reasoning = self._resolve_bool_override(
                    section,
                    "show_reasoning",
                    self.config.channels.show_reasoning,
                )
                self.channels[name] = channel
                logger.info("{} channel enabled", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _resolve_transcription_config_value(self, section: str, field: str) -> str | None:
        """Fallback read of ``config.providers.<section>.<field>`` for old callers.

        Kept only for the legacy ``ChannelManager(config, bus)`` construction:
        the composition root now injects these values explicitly.  Defensive
        ``getattr`` replaces the old ``try/except AttributeError`` — tests build
        managers over partial config objects (``SimpleNamespace``) that may lack
        the ``providers`` section or the provider entry entirely; a missing value
        resolves to ``None`` and the ``_transcription`` helpers normalize it to
        ``""``.
        """
        try:
            return getattr(
                getattr(getattr(self.config, "providers", None), section, None),
                field,
                None,
            )
        except AttributeError:
            return None

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            cfg = ch.config
            if isinstance(cfg, dict):
                if "allow_from" in cfg:
                    allow = cfg.get("allow_from")
                else:
                    allow = cfg.get("allowFrom")
            else:
                allow = getattr(cfg, "allow_from", None)
            if allow is None:
                # allowFrom omitted → every sender is denied (no pairing
                # fallback anymore). Surface the misconfiguration loudly.
                logger.warning(
                    '"{}" has no allowFrom; all senders will be denied until you set allowFrom',
                    name,
                )

    def _should_send_progress(self, channel_name: str, *, tool_hint: bool = False) -> bool:
        """Return whether progress (or tool-hints) may be sent to *channel_name*."""
        ch = self.channels.get(channel_name)
        if ch is None:
            logger.warning("Progress check for unknown channel: {}", channel_name)
            return False
        return ch.send_tool_hints if tool_hint else ch.send_progress

    def _resolve_bool_override(self, section: Any, key: str, default: bool) -> bool:
        """Return *key* from *section* if it is a bool, otherwise *default*.

        For dict configs also checks the camelCase alias (e.g. ``sendProgress``
        for ``send_progress``) so raw JSON/TOML configs work alongside
        Pydantic models.
        """
        if isinstance(section, dict):
            value = section.get(key)
            if value is None:
                camel = _BOOL_CAMEL_ALIASES.get(key)
                if camel:
                    value = section.get(camel)
            return value if isinstance(value, bool) else default
        value = getattr(section, key, None)
        return value if isinstance(value, bool) else default

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception:
            logger.exception("Failed to start channel {}", name)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher.

        Idempotent with respect to the dispatcher: calling twice without an
        intervening ``stop_all`` must not orphan the first dispatch task (the
        old task would keep consuming the bus with no reference held, and
        ``stop_all`` would only cancel the newest one).
        """
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher (only once)
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        else:
            logger.debug("Outbound dispatcher already running; not starting a second one")

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        self._notify_restart_done_if_needed()

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    def _notify_restart_done_if_needed(self) -> None:
        """Send restart completion message when runtime env markers are present."""
        notice = consume_restart_notice_from_env()
        if not notice:
            return
        target = self.channels.get(notice.channel)
        if not target:
            return
        task = asyncio.create_task(
            self._send_with_retry(
                target,
                OutboundMessage(
                    channel=notice.channel,
                    chat_id=notice.chat_id,
                    content=format_restart_completed_message(notice.started_at_raw),
                    metadata=dict(notice.metadata or {}),
                ),
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher.

        Each channel stop is wrapped to catch ``BaseException`` (including
        ``asyncio.CancelledError``) so that a cancellation observed by one
        ``channel.stop()`` does not escape and abort the loop before other
        channels have been stopped.
        """
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatch_task
            self._dispatch_task = None

        # 取消仍存活的后台发送任务 (restart 通知等 fire-and-forget 任务)。
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Stop all channels. Catch BaseException (not just Exception) so that
        # CancelledError observed during shutdown does not skip remaining
        # channels. We re-raise the CancelledError after all channels are
        # stopped to preserve cancellation semantics.
        cancelled: asyncio.CancelledError | None = None
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except asyncio.CancelledError as exc:
                # Defer re-raise until all channels have been stopped.
                if cancelled is None:
                    cancelled = exc
                logger.warning("Cancelled while stopping {} channel", name)
            except Exception:
                logger.exception("Error stopping {}", name)
        if cancelled is not None:
            raise cancelled

    @staticmethod
    def _fingerprint_content(content: str) -> str:
        normalized = " ".join(content.split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest() if normalized else ""

    def _should_suppress_outbound(self, msg: OutboundMessage) -> bool:
        metadata = msg.metadata or {}
        if metadata.get("_progress"):
            return False
        fingerprint = self._fingerprint_content(msg.content)
        if not fingerprint:
            return False

        origin_message_id = metadata.get("origin_message_id")
        if isinstance(origin_message_id, str) and origin_message_id:
            key = (msg.channel, msg.chat_id, origin_message_id)
            # 命中时 move_to_end 维护 LRU 顺序,使最近访问的 key 不被优先淘汰。
            # 仅当容器支持 LRU 排序(OrderedDict)时才执行 move_to_end,允许
            # 测试用普通 dict 替换该字段而不破坏基本去重行为。
            if self._origin_reply_fingerprints.get(key) == fingerprint:
                if hasattr(self._origin_reply_fingerprints, "move_to_end"):
                    self._origin_reply_fingerprints.move_to_end(key)
                return True
            self._record_origin_reply_fingerprint(key, fingerprint)

        message_id = metadata.get("message_id")
        if isinstance(message_id, str) and message_id:
            key = (msg.channel, msg.chat_id, message_id)
            self._record_origin_reply_fingerprint(key, fingerprint)

        return False

    def _record_origin_reply_fingerprint(self, key: tuple[str, str, str], fingerprint: str) -> None:
        """写入去重指纹缓存并按 LRU 策略淘汰最旧条目。

        新写入或覆盖的 key 都会 move_to_end,确保最近写入的 key 不会被
        优先淘汰;当条目数超过 ``_MAX_ORIGIN_REPLY_FINGERPRINTS`` 时,
        从头部弹出最旧的 key。当容器为普通 dict(不支持 LRU 排序)时,
        仅写入不维护顺序,允许测试用普通 dict 替换该字段。
        """
        self._origin_reply_fingerprints[key] = fingerprint
        if not hasattr(self._origin_reply_fingerprints, "move_to_end"):
            return
        self._origin_reply_fingerprints.move_to_end(key)
        while len(self._origin_reply_fingerprints) > self._MAX_ORIGIN_REPLY_FINGERPRINTS:
            self._origin_reply_fingerprints.popitem(last=False)

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        # Buffer for messages that couldn't be processed during delta coalescing
        # (since asyncio.Queue doesn't support push_front)
        pending: list[OutboundMessage] = []

        while True:
            try:
                # First check pending buffer before waiting on queue
                if pending:
                    msg = pending.pop(0)
                else:
                    msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)

                if (
                    msg.metadata.get("_reasoning_delta")
                    or msg.metadata.get("_reasoning_end")
                    or msg.metadata.get("_reasoning")
                ):
                    # Reasoning rides its own plugin channel: only delivered
                    # when the destination channel opts in via ``show_reasoning``
                    # and overrides the streaming primitives. Channels without
                    # a low-emphasis UI affordance keep the base no-op and the
                    # content silently drops here. ``_reasoning`` (one-shot)
                    # is accepted for backward compatibility with hooks that
                    # haven't migrated to delta/end yet.
                    channel = self.channels.get(msg.channel)
                    if channel is not None and channel.show_reasoning:
                        await self._send_with_retry(channel, msg)
                    continue

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel,
                        tool_hint=True,
                    ):
                        continue
                    if not msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel,
                        tool_hint=False,
                    ):
                        continue

                if msg.metadata.get("_retry_wait"):
                    continue

                if (
                    msg.metadata.get("_runtime_model_updated")
                    and msg.channel == "websocket"
                    and "websocket" not in self.channels
                ):
                    continue

                # Coalesce consecutive _stream_delta messages for the same (channel, chat_id)
                # to reduce API calls and improve streaming latency
                if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)

                channel = self.channels.get(msg.channel)
                if channel:
                    # Duplicate suppression is scoped to a known source message
                    # so repeated content from separate turns is still delivered.
                    if (
                        not msg.metadata.get("_stream_delta")
                        and not msg.metadata.get("_stream_end")
                        and not msg.metadata.get("_streamed")
                    ):
                        if self._should_suppress_outbound(msg):
                            logger.info(
                                "Suppressing duplicate outbound message to {}:{}",
                                msg.channel,
                                msg.chat_id,
                            )
                            continue
                    await self._send_with_retry(channel, msg)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                # Propagate cancellation instead of swallowing it. Returning
                # normally would leave ``task.cancelled()`` False and mask the
                # shutdown request from ``stop_all``/``TaskGroup`` callers.
                if pending:
                    logger.debug(
                        "Outbound dispatcher cancelled with {} buffered message(s) undelivered",
                        len(pending),
                    )
                raise

    @staticmethod
    async def _send_once(channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send one outbound message without retry policy."""
        if msg.metadata.get("_reasoning_end"):
            await channel.send_reasoning_end(msg.chat_id, msg.metadata)
        elif msg.metadata.get("_reasoning_delta"):
            await channel.send_reasoning_delta(msg.chat_id, msg.content, msg.metadata)
        elif msg.metadata.get("_reasoning"):
            # Back-compat: one-shot reasoning. BaseChannel translates this
            # to a single delta + end pair so plugins only implement the
            # streaming primitives.
            await channel.send_reasoning(msg)
        elif msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end"):
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
        elif not msg.metadata.get("_streamed"):
            await channel.send(msg)

    def _coalesce_stream_deltas(
        self, first_msg: OutboundMessage
    ) -> tuple[OutboundMessage, list[OutboundMessage]]:
        """Merge consecutive _stream_delta messages for the same (channel, chat_id).

        This reduces the number of API calls when the queue has accumulated multiple
        deltas, which happens when LLM generates faster than the channel can process.

        Returns:
            tuple of (merged_message, list_of_non_matching_messages)
        """
        target_key = (first_msg.channel, first_msg.chat_id)
        combined_content = first_msg.content
        final_metadata = dict(first_msg.metadata or {})
        non_matching: list[OutboundMessage] = []

        # Only merge consecutive deltas. As soon as we hit any other message,
        # stop and hand that boundary back to the dispatcher via `pending`.
        while True:
            try:
                next_msg = self.bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Check if this message belongs to the same stream
            same_target = (next_msg.channel, next_msg.chat_id) == target_key
            is_delta = next_msg.metadata and next_msg.metadata.get("_stream_delta")
            is_end = next_msg.metadata and next_msg.metadata.get("_stream_end")

            if same_target and is_delta and not final_metadata.get("_stream_end"):
                # Accumulate content
                combined_content += next_msg.content
                # If we see _stream_end, remember it and stop coalescing this stream
                if is_end:
                    final_metadata["_stream_end"] = True
                    # Stream ended - stop coalescing this stream
                    break
            else:
                # First non-matching message defines the coalescing boundary.
                non_matching.append(next_msg)
                break

        merged = OutboundMessage(
            channel=first_msg.channel,
            chat_id=first_msg.chat_id,
            content=combined_content,
            metadata=final_metadata,
        )
        return merged, non_matching

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send a message with retry on failure using exponential backoff.

        Each attempt is bounded by ``config.channels.send_timeout_s`` (0
        disables).  The outbound dispatcher is a single task shared by *all*
        channels, so a channel whose ``send`` never returns — e.g. a websocket
        client that stops reading and lets the TCP send buffer fill — would
        otherwise freeze delivery for every other channel.  A timed-out attempt
        is treated like any other send failure and retried.

        Note: CancelledError is re-raised to allow graceful shutdown.
        """
        max_attempts = max(self.config.channels.send_max_retries, 1)
        send_timeout = getattr(self.config.channels, "send_timeout_s", 0) or 0

        for attempt in range(max_attempts):
            try:
                await self._send_once_timed(channel, msg, send_timeout)
                return  # Send succeeded
            except asyncio.CancelledError:
                raise  # Propagate cancellation for graceful shutdown
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.exception(
                        "Failed to send to {} after {} attempts", msg.channel, max_attempts
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt, len(_SEND_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Send to {} failed (attempt {}/{}): {}, retrying in {}s",
                    msg.channel,
                    attempt + 1,
                    max_attempts,
                    type(e).__name__,
                    delay,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise  # Propagate cancellation during sleep

    @classmethod
    async def _send_once_timed(
        cls,
        channel: BaseChannel,
        msg: OutboundMessage,
        timeout_s: float,
    ) -> None:
        """Run :meth:`_send_once` under an optional wall-clock timeout.

        ``timeout_s <= 0`` disables the bound (legacy behaviour).
        """
        if timeout_s <= 0:
            await cls._send_once(channel, msg)
            return
        try:
            await asyncio.wait_for(cls._send_once(channel, msg), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "Send to {}:{} timed out after {}s (channel={}); treating as failure",
                channel.__class__.__name__,
                msg.chat_id,
                timeout_s,
                msg.channel,
            )
            raise

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {"enabled": True, "running": channel.is_running}
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
