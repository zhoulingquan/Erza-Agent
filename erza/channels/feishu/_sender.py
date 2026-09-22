"""Feishu channel send/reply sync mixin.

Splits the reply + send synchronous cluster out of ``channel.py``: content
fetching, reply/create-message helpers, interactive-card fallbacks and the
CardKit streaming-control helpers.  The module-level ``_STREAM_ELEMENT_ID``
constant is resolved at call time through the ``channel`` module namespace so
the module keeps a single source of truth for its constants.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from erza.channels.feishu import channel as _ch
from erza.channels.feishu._content_extract import (
    _extract_interactive_content,
    _extract_post_content,
)


class SendSyncMixin:
    _REPLY_CONTEXT_MAX_LEN = 200

    def _get_message_content_sync(self, message_id: str) -> str | None:
        """Fetch the text content of a Feishu message by ID (synchronous).

        Returns a "[Reply to: ...]" context string, or None on failure.
        """
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                self.logger.debug(
                    "could not fetch parent message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return None
            items = getattr(response.data, "items", None)
            if not items:
                return None
            msg_obj = items[0]
            raw_content = getattr(msg_obj, "body", None)
            raw_content = getattr(raw_content, "content", None) if raw_content else None
            if not raw_content:
                return None
            try:
                content_json = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None
            msg_type = getattr(msg_obj, "msg_type", "")
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                text = text.strip()
            else:
                text = ""
            if not text:
                return None
            if len(text) > self._REPLY_CONTEXT_MAX_LEN:
                text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
            return f"[Reply to: {text}]"
        except Exception as e:
            self.logger.debug("error fetching parent message {}: {}", message_id, e)
            return None

    def _reply_message_sync(
        self, parent_message_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False
    ) -> bool:
        """Reply to an existing Feishu message using the Reply API (synchronous).

        Args:
            reply_in_thread: If True, reply as a thread/topic message
                in the Feishu client.
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            body_builder = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
            if reply_in_thread:
                body_builder = body_builder.reply_in_thread(True)
            request = (
                ReplyMessageRequest.builder()
                .message_id(parent_message_id)
                .request_body(body_builder.build())
                .build()
            )
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                self.logger.error(
                    "Failed to reply to message {}: code={}, msg={}, log_id={}",
                    parent_message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                if msg_type == "interactive":
                    return self._reply_interactive_fallback_sync(
                        parent_message_id,
                        content,
                        reply_in_thread=reply_in_thread,
                    )
                return False
            self.logger.debug("reply sent to message {}", parent_message_id)
            return True
        except Exception:
            self.logger.exception("Error replying to message {}", parent_message_id)
            if msg_type == "interactive":
                return self._reply_interactive_fallback_sync(
                    parent_message_id,
                    content,
                    reply_in_thread=reply_in_thread,
                )
            return False

    @staticmethod
    def _interactive_content_to_text(content: str) -> str | None:
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
        parts = [part.strip() for part in _extract_interactive_content(payload) if part.strip()]
        text = "\n".join(parts).strip()
        return text or None

    @staticmethod
    def _fallback_text_chunks(text: str, limit: int = 3500) -> list[str]:
        text = text.strip()
        if not text:
            return []
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [chunk for chunk in chunks if chunk]

    def _reply_interactive_fallback_sync(
        self,
        parent_message_id: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> bool:
        text = self._interactive_content_to_text(content)
        if not text:
            return False
        sent = False
        for chunk in self._fallback_text_chunks(text):
            body = json.dumps({"text": chunk}, ensure_ascii=False)
            sent = (
                self._reply_message_sync(
                    parent_message_id,
                    "text",
                    body,
                    reply_in_thread=reply_in_thread,
                )
                or sent
            )
        if sent:
            self.logger.warning("Sent Feishu interactive reply as text fallback")
        return sent

    def _send_interactive_fallback_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        content: str,
    ) -> str | None:
        text = self._interactive_content_to_text(content)
        if not text:
            return None
        last_message_id: str | None = None
        for chunk in self._fallback_text_chunks(text):
            body = json.dumps({"text": chunk}, ensure_ascii=False)
            message_id = self._send_message_sync(receive_id_type, receive_id, "text", body)
            if message_id:
                last_message_id = message_id
        if last_message_id:
            self.logger.warning("Sent Feishu interactive message as text fallback")
        return last_message_id

    def _should_use_reply_in_thread(self, metadata: dict[str, Any]) -> bool:
        """Return whether a group reply should create a Feishu thread/topic."""
        return metadata.get("chat_type", "group") == "group" and self.config.reply_to_message

    def _thread_reply_target(self, metadata: dict[str, Any]) -> str | None:
        """Return the message_id that should receive a Reply API response."""
        if metadata.get("chat_type", "group") != "group":
            return None
        message_id = metadata.get("message_id")
        if not message_id:
            return None
        if metadata.get("thread_id") or self.config.reply_to_message:
            return message_id
        return None

    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> str | None:
        """Send a single message and return the message_id on success."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                self.logger.error(
                    "Failed to send {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                if msg_type == "interactive":
                    return self._send_interactive_fallback_sync(
                        receive_id_type,
                        receive_id,
                        content,
                    )
                return None
            msg_id = getattr(response.data, "message_id", None)
            self.logger.debug("{} message sent to {}: {}", msg_type, receive_id, msg_id)
            return msg_id
        except Exception:
            self.logger.exception("Error sending {} message", msg_type)
            return None

    def _create_streaming_card_sync(
        self,
        receive_id_type: str,
        chat_id: str,
        reply_message_id: str | None = None,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Create a CardKit streaming card, send it to chat, return card_id.

        When *reply_message_id* is provided the card is delivered via the
        reply API. *reply_in_thread* controls whether Feishu creates a
        thread/topic for that reply. Otherwise the plain create-message API is
        used.
        """
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        card_json = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "", "element_id": _ch._STREAM_ELEMENT_ID}
                ]
            },
        }
        try:
            request = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.create(request)
            if not response.success():
                self.logger.warning(
                    "Failed to create streaming card: code={}, msg={}", response.code, response.msg
                )
                return None
            card_id = getattr(response.data, "card_id", None)
            if card_id:
                card_content = json.dumps(
                    {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
                )
                if reply_message_id:
                    sent = self._reply_message_sync(
                        reply_message_id,
                        "interactive",
                        card_content,
                        reply_in_thread=reply_in_thread,
                    )
                else:
                    sent = (
                        self._send_message_sync(
                            receive_id_type,
                            chat_id,
                            "interactive",
                            card_content,
                        )
                        is not None
                    )
                if sent:
                    return card_id
                self.logger.warning(
                    "Created streaming card {} but failed to send it to {}", card_id, chat_id
                )
            return None
        except Exception as e:
            self.logger.warning("Error creating streaming card: {}", e)
            return None

    def _stream_update_text_sync(self, card_id: str, content: str, sequence: int) -> bool:
        """Stream-update the markdown element on a CardKit card (typewriter effect)."""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        try:
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(_ch._STREAM_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                self.logger.warning(
                    "Failed to stream-update card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error stream-updating card {}: {}", card_id, e)
            return False

    def _set_streaming_mode_sync(self, card_id: str, enabled: bool, sequence: int) -> bool:
        """Set CardKit streaming_mode using a strictly increasing sequence."""
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        settings_payload = json.dumps({"config": {"streaming_mode": enabled}}, ensure_ascii=False)
        try:
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_payload)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.settings(request)
            if not response.success():
                self.logger.warning(
                    "Failed to set streaming={} on card {}: code={}, msg={}",
                    enabled,
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error setting streaming={} on card {}: {}", enabled, card_id, e)
            return False

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
        """Turn off CardKit streaming_mode so the chat list preview exits the streaming placeholder.

        Per Feishu docs, streaming cards keep a generating-style summary in the session list until
        streaming_mode is set to false via card settings (after final content update).
        Sequence must strictly exceed the previous card OpenAPI operation on this entity.
        """
        return self._set_streaming_mode_sync(card_id, False, sequence)

    def _stream_update_text_with_reopen_sync(
        self,
        card_id: str,
        content: str,
        sequence: int,
    ) -> tuple[bool, int]:
        if self._stream_update_text_sync(card_id, content, sequence):
            return True, sequence
        sequence += 1
        if not self._set_streaming_mode_sync(card_id, True, sequence):
            return False, sequence
        sequence += 1
        return self._stream_update_text_sync(card_id, content, sequence), sequence
