"""Feishu channel inbound event + tool-hint formatting mixin.

Splits the WebSocket event entry points and inline tool-hint formatting out of
``channel.py``.  Module-level constants that still live on ``channel.py``
(``MSG_TYPE_MAP``, ``_NEW_SESSION_DIVIDER_CONTENT``) are resolved at call time
through the ``channel`` namespace so the module keeps one source of truth.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from erza.channels.feishu import channel as _ch
from erza.channels.feishu._content_extract import (
    _extract_post_content,
    _extract_share_card_content,
)
from erza.command.router import normalize_command_text

if TYPE_CHECKING:
    from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1


class EventMixin:
    def _on_message_sync(self, data: Any) -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if not self._running:
            return
        loop = self._loop
        if loop is not None and not loop.is_closed() and loop.is_running():
            # 保存 future 并挂 done 回调:否则 future 无引用会被 GC,协程内部
            # 抛出的异常只会以 "exception was never retrieved" 的形式丢失,
            # 且 loop 关闭后提交会抛 RuntimeError 到 SDK 线程。
            with suppress(RuntimeError):
                future = asyncio.run_coroutine_threadsafe(self._on_message(data), loop)
                future.add_done_callback(self._log_background_future_result)

    def _log_background_future_result(self, future: Any) -> None:
        """Retrieve and log the result of a cross-thread coroutine submission."""
        if future.cancelled():
            return
        with suppress(Exception):
            error = future.exception()
            if error is not None:
                self.logger.error("Error handling Feishu message: {}", error)

    async def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """Handle incoming message from Feishu."""
        if not self._running:
            return
        try:
            event = data.event
            message = event.message
            sender = event.sender

            self.logger.debug("raw message: {}", message.content)
            self.logger.debug("mentions: {}", getattr(message, "mentions", None))

            message_id = message.message_id

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            if chat_type == "group" and not self._is_group_message_for_bot(message):
                self.logger.debug("skipping group message (not mentioned)")
                return

            # Deduplication check
            if not self._dedup_message(message_id):
                return

            # Early permission check — avoid side effects for unauthorized users.
            # Group chats and DMs are silently ignored; access is managed
            # exclusively via the allowFrom config.
            if not self.is_allowed(sender_id):
                self.logger.warning(
                    "Access denied for sender {}. "
                    "Add them to allowFrom list in config to grant access.",
                    sender_id,
                )
                return

            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(self._add_reaction(message_id, self.config.react_emoji))
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    mentions = getattr(message, "mentions", None)
                    text = self._strip_leading_bot_mention(text, mentions)
                    text = self._resolve_mentions(text, mentions)
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(
                    msg_type, content_json, message_id
                )
                if file_path:
                    media_paths.append(file_path)

                if msg_type == "audio" and file_path:
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        content_text = f"[transcription: {transcription}]"

                content_parts.append(content_text)

            elif msg_type in (
                "share_chat",
                "share_user",
                "interactive",
                "share_calendar_event",
                "system",
                "merge_forward",
            ):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(_ch.MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            # Extract reply context (parent/root message IDs)
            parent_id = getattr(message, "parent_id", None) or None
            root_id = getattr(message, "root_id", None) or None
            thread_id = getattr(message, "thread_id", None) or None

            # Prepend quoted message text when the user replied to another message
            if parent_id and self._client:
                loop = asyncio.get_running_loop()
                reply_ctx = await loop.run_in_executor(
                    None, self._get_message_content_sync, parent_id
                )
                if reply_ctx:
                    content_parts.insert(0, reply_ctx)

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            if chat_type == "p2p" and normalize_command_text(content).lower() == "/new":
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self._send_message_sync,
                    "open_id",
                    sender_id,
                    "system",
                    _ch._NEW_SESSION_DIVIDER_CONTENT,
                )

            # Build session key for conversation isolation.
            # If topic_isolation is True: each topic gets its own session via root_id/message_id.
            # If topic_isolation is False: all messages in group share the same session.
            # Private chat: no override — same behavior as Telegram/Slack.
            if chat_type == "group":
                if self.config.topic_isolation:
                    session_key = f"{self.name}:{chat_id}:{root_id or message_id}"
                else:
                    session_key = f"{self.name}:{chat_id}"
            else:
                session_key = None

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                },
                session_key=session_key,
            )

        except Exception:
            self.logger.exception("Error processing message")

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction events so they do not generate SDK noise."""
        pass

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction deleted events so they do not generate SDK noise."""
        pass

    def _on_message_read(self, data: Any) -> None:
        """Ignore read events so they do not generate SDK noise."""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Ignore p2p-enter events when a user opens a bot chat."""
        self.logger.debug("Bot entered p2p chat (user opened chat window)")
        pass

    @staticmethod
    def _format_tool_hint_lines(tool_hint: str) -> str:
        """Split tool hints across lines on top-level call separators only."""
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False

        for i, ch in enumerate(tool_hint):
            buf.append(ch)

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
                continue

            if ch in {'"', "'"}:
                in_string = True
                quote_char = ch
                continue

            if ch == "(":
                depth += 1
                continue

            if ch == ")" and depth > 0:
                depth -= 1
                continue

            if ch == "," and depth == 0:
                next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
                if next_char == " ":
                    parts.append("".join(buf).rstrip())
                    buf = []

        if buf:
            parts.append("".join(buf).strip())

        return "\n".join(part for part in parts if part)

    def _format_tool_hint_delta(self, tool_hint: str) -> str:
        """Format a tool hint string with the 🔧 prefix for each line."""
        lines = self.__class__._format_tool_hint_lines(tool_hint).split("\n")
        return "\n".join(f"{self.config.tool_hint_prefix} {ln}" for ln in lines if ln.strip())
