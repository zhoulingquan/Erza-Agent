"""WeChat channel outbound sending mixin.

Splits the send cluster out of ``channel.py`` (``send`` / ``send_delta`` /
typing indicator control / ``_send_text`` / ``_send_media_file``).
Patchable constants and the shared AES/media helpers are resolved at call
time through the ``channel`` module namespace so test monkeypatches keep
working.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from erza.bus.events import OutboundMessage
from erza.channels.weixin import channel as _ch
from erza.utils.helpers import split_message


class SendMixin:
    async def send(self, msg: OutboundMessage) -> None:
        if not self._client or not self._token:
            raise RuntimeError("WeChat client not initialized or not authenticated")
        self._assert_session_active()

        progress_event = msg.metadata.get("_progress")
        is_progress = bool(progress_event)

        # Buffer tool hints to coalesce consecutive ones and avoid burning
        # WeChat iLink rate-limit quota (~7 msgs / 5 min).
        if progress_event and msg.metadata.get("_tool_hint"):
            if not self.send_tool_hints:
                return
            self._pending_tool_hints.setdefault(msg.chat_id, []).append(msg.content)
            self.logger.debug(
                "Buffered tool hint for {} (count={})",
                msg.chat_id,
                len(self._pending_tool_hints[msg.chat_id]),
            )
            return

        # Reasoning deltas are invisible in WeChat (there is no reasoning
        # UI).  Skip them entirely — do not send and do not flush buffer.
        if progress_event and (
            msg.metadata.get("_reasoning_delta") or msg.metadata.get("_reasoning")
        ):
            self.logger.debug("Dropped invisible reasoning delta for {}", msg.chat_id)
            return

        content = msg.content.strip()

        # Empty progress messages (e.g. after_iteration tool_events) must
        # NOT act as separators — they have no visible content.
        if is_progress and not content and not (msg.media or []):
            self.logger.debug(
                "Skipped empty progress message for {} (no visible content)",
                msg.chat_id,
            )
            return

        # Flush buffered hints before sending any visible message.
        await self._flush_tool_hints(msg.chat_id)

        if not is_progress:
            await self._stop_typing(msg.chat_id, clear_remote=True)

        ctx_token = self._context_tokens.get(msg.chat_id, "")
        ctx_token = await self._refresh_context_token_if_stale(msg.chat_id, ctx_token)
        if not ctx_token:
            raise RuntimeError(
                f"WeChat context_token missing for chat_id={msg.chat_id}, cannot send"
            )

        typing_ticket = ""
        with suppress(Exception):
            typing_ticket = await self._get_typing_ticket(msg.chat_id, ctx_token)

        if typing_ticket:
            with suppress(Exception):
                await self._send_typing(msg.chat_id, typing_ticket, _ch.TYPING_STATUS_TYPING)

        typing_keepalive_stop = asyncio.Event()
        typing_keepalive_task: asyncio.Task | None = None
        if typing_ticket:
            typing_keepalive_task = asyncio.create_task(
                self._typing_keepalive_loop(msg.chat_id, typing_ticket, typing_keepalive_stop)
            )

        try:
            # --- Send media files first (following Telegram channel pattern) ---
            for media_path in msg.media or []:
                try:
                    await self._send_media_file(msg.chat_id, media_path, ctx_token)
                except (httpx.TimeoutException, httpx.TransportError):
                    # Network/transport errors: do NOT fall back to text —
                    # the text send would also likely fail, and the outer
                    # except will re-raise so ChannelManager retries properly.
                    self.logger.opt(exception=True).warning(
                        "Network error sending media {}",
                        media_path,
                    )
                    raise
                except httpx.HTTPStatusError as http_err:
                    status_code = (
                        http_err.response.status_code if http_err.response is not None else 0
                    )
                    if status_code >= 500:
                        # Server-side / retryable HTTP error — same as network.
                        self.logger.exception(
                            "Server error ({} {}) sending media {}",
                            status_code,
                            http_err.response.reason_phrase
                            if http_err.response is not None
                            else "",
                            media_path,
                        )
                        raise
                    # 4xx client errors are NOT retryable — fall back to text.
                    filename = Path(media_path).name
                    self.logger.exception("Failed to send media {}", media_path)
                    await self._send_text(
                        msg.chat_id,
                        f"[Failed to send: {filename}]",
                        ctx_token,
                    )
                except Exception:
                    # Non-network errors (format, file-not-found, etc.):
                    # notify the user via text fallback.
                    filename = Path(media_path).name
                    self.logger.exception("Failed to send media {}", media_path)
                    # Notify user about failure via text
                    await self._send_text(
                        msg.chat_id,
                        f"[Failed to send: {filename}]",
                        ctx_token,
                    )

            # --- Send text content ---
            if not content:
                return

            chunks = split_message(content, _ch.WEIXIN_MAX_MESSAGE_LEN)
            for chunk in chunks:
                await self._send_text(msg.chat_id, chunk, ctx_token)
        except Exception:
            self.logger.exception("Error sending message")
            raise
        finally:
            if typing_keepalive_task:
                typing_keepalive_stop.set()
                typing_keepalive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_keepalive_task

            if typing_ticket and not is_progress:
                with suppress(Exception):
                    await self._send_typing(msg.chat_id, typing_ticket, _ch.TYPING_STATUS_CANCEL)

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        """Deliver a streamed reply to WeChat.

        WeChat iLink has no native incremental delivery, and the manager
        bypasses :meth:`send` for the ``_streamed`` final answer. So we
        accumulate content deltas and flush the full reply as a single message
        at stream end. Reasoning deltas are invisible in WeChat and are dropped.
        """
        meta = metadata or {}
        if meta.get("_reasoning_delta") or meta.get("_reasoning"):
            return
        is_end = stream_end or bool(meta.get("_stream_end"))
        buffer_key = stream_id or chat_id
        # Accumulate intermediate deltas. The stream_end message's own content
        # (present when the manager coalesces deltas into the end message) is
        # folded into `full` below instead of appended here, so a send retry
        # recomputes the same `full` from an unchanged buffer rather than
        # double-counting that delta.
        if delta and not is_end:
            self._stream_buffers.setdefault(buffer_key, []).append(delta)
        if not is_end:
            return
        full = ("".join(self._stream_buffers.get(buffer_key, [])) + (delta or "")).strip()
        await self._flush_tool_hints(chat_id)
        if full:
            # Send before clearing the buffer: if the send raises, the buffer is
            # left intact so ChannelManager._send_with_retry can re-deliver the
            # same stream_end message instead of silently losing the reply.
            await self.send(OutboundMessage(channel=self.name, chat_id=chat_id, content=full))
        self._stream_buffers.pop(buffer_key, None)

    async def _start_typing(self, chat_id: str, context_token: str = "") -> None:
        """Start typing indicator immediately when a message is received."""
        if not self._client or not self._token or not chat_id:
            return
        await self._stop_typing(chat_id, clear_remote=False)
        try:
            ticket = await self._get_typing_ticket(chat_id, context_token)
            if not ticket:
                return
            await self._send_typing(chat_id, ticket, _ch.TYPING_STATUS_TYPING)
        except Exception as e:
            self.logger.debug("typing indicator start failed for {}: {}", chat_id, e)
            return

        stop_event = asyncio.Event()

        async def keepalive() -> None:
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(_ch.TYPING_KEEPALIVE_INTERVAL_S)
                    if stop_event.is_set():
                        break
                    with suppress(Exception):
                        await self._send_typing(chat_id, ticket, _ch.TYPING_STATUS_TYPING)
            finally:
                pass

        task = asyncio.create_task(keepalive())
        task._typing_stop_event = stop_event  # type: ignore[attr-defined]
        self._typing_tasks[chat_id] = task

    async def _stop_typing(self, chat_id: str, *, clear_remote: bool) -> None:
        """Stop typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            stop_event = getattr(task, "_typing_stop_event", None)
            if stop_event:
                stop_event.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if not clear_remote:
            return
        entry = self._typing_tickets.get(chat_id)
        ticket = str(entry.get("ticket", "") or "") if isinstance(entry, dict) else ""
        if not ticket:
            return
        try:
            await self._send_typing(chat_id, ticket, _ch.TYPING_STATUS_CANCEL)
        except Exception as e:
            self.logger.debug("typing clear failed for {}: {}", chat_id, e)

    async def _send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> None:
        """Send a text message matching the exact protocol from send.ts."""
        client_id = f"erza-{uuid.uuid4().hex[:12]}"

        item_list: list[dict] = []
        if text:
            item_list.append({"type": _ch.ITEM_TEXT, "text_item": {"text": text}})

        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": _ch.MESSAGE_TYPE_BOT,
            "message_state": _ch.MESSAGE_STATE_FINISH,
        }
        if item_list:
            weixin_msg["item_list"] = item_list
        if context_token:
            weixin_msg["context_token"] = context_token

        body: dict[str, Any] = {
            "msg": weixin_msg,
            "base_info": _ch.BASE_INFO,
        }

        data = await self._api_post("ilink/bot/sendmessage", body)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"WeChat send text error (ret={ret}, errcode={errcode}): {data.get('errmsg', '')}"
            )

    async def _send_media_file(
        self,
        to_user_id: str,
        media_path: str,
        context_token: str,
    ) -> None:
        """Upload a local file to WeChat CDN and send it as a media message.

        Follows the exact protocol from ``@tencent-weixin/openclaw-weixin`` v1.0.3:
        1. Generate a random 16-byte AES key (client-side).
        2. Call ``getuploadurl`` with file metadata + hex-encoded AES key.
        3. AES-128-ECB encrypt the file and POST to CDN (``{cdnBaseUrl}/upload``).
        4. Read ``x-encrypted-param`` header from CDN response as the download param.
        5. Send a ``sendmessage`` with the appropriate media item referencing the upload.
        """
        p = Path(media_path)
        if not p.is_file():
            raise FileNotFoundError(f"Media file not found: {media_path}")

        raw_data = p.read_bytes()
        raw_size = len(raw_data)
        raw_md5 = hashlib.md5(raw_data).hexdigest()

        # Determine upload media type from extension
        ext = p.suffix.lower()
        if ext in _ch._IMAGE_EXTS:
            upload_type = _ch.UPLOAD_MEDIA_IMAGE
            item_type = _ch.ITEM_IMAGE
            item_key = "image_item"
        elif ext in _ch._VIDEO_EXTS:
            upload_type = _ch.UPLOAD_MEDIA_VIDEO
            item_type = _ch.ITEM_VIDEO
            item_key = "video_item"
        elif ext in _ch._VOICE_EXTS:
            upload_type = _ch.UPLOAD_MEDIA_VOICE
            item_type = _ch.ITEM_VOICE
            item_key = "voice_item"
        else:
            upload_type = _ch.UPLOAD_MEDIA_FILE
            item_type = _ch.ITEM_FILE
            item_key = "file_item"

        # Generate client-side AES-128 key (16 random bytes)
        aes_key_raw = os.urandom(16)
        aes_key_hex = aes_key_raw.hex()

        # Compute encrypted size: PKCS7 padding to 16-byte boundary
        # Matches aesEcbPaddedSize: Math.ceil((size + 1) / 16) * 16
        padded_size = ((raw_size + 1 + 15) // 16) * 16

        # Step 1: Get upload URL from server (prefer upload_full_url, fallback to upload_param)
        file_key = os.urandom(16).hex()
        upload_body: dict[str, Any] = {
            "filekey": file_key,
            "media_type": upload_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": padded_size,
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
        }

        assert self._client is not None
        upload_resp = await self._api_post("ilink/bot/getuploadurl", upload_body)

        upload_full_url = str(upload_resp.get("upload_full_url", "") or "").strip()
        upload_param = str(upload_resp.get("upload_param", "") or "")
        if not upload_full_url and not upload_param:
            raise RuntimeError(
                "getuploadurl returned no upload URL "
                f"(need upload_full_url or upload_param): {upload_resp}"
            )

        # Step 2: AES-128-ECB encrypt and POST to CDN
        aes_key_b64 = base64.b64encode(aes_key_raw).decode()
        encrypted_data = _ch._encrypt_aes_ecb(raw_data, aes_key_b64)

        if upload_full_url:
            cdn_upload_url = upload_full_url
        else:
            cdn_upload_url = (
                f"{self.config.cdn_base_url}/upload"
                f"?encrypted_query_param={quote(upload_param)}"
                f"&filekey={quote(file_key)}"
            )

        cdn_resp = await self._client.post(
            cdn_upload_url,
            content=encrypted_data,
            headers={"Content-Type": "application/octet-stream"},
        )
        cdn_resp.raise_for_status()

        # The download encrypted_query_param comes from CDN response header
        download_param = cdn_resp.headers.get("x-encrypted-param", "")
        if not download_param:
            raise RuntimeError(
                "CDN upload response missing x-encrypted-param header; "
                f"status={cdn_resp.status_code} headers={dict(cdn_resp.headers)}"
            )

        # Step 3: Send message with the media item
        # aes_key for CDNMedia is the hex key encoded as base64
        # (matches: Buffer.from(uploaded.aeskey).toString("base64"))
        cdn_aes_key_b64 = base64.b64encode(aes_key_hex.encode()).decode()

        media_item: dict[str, Any] = {
            "media": {
                "encrypt_query_param": download_param,
                "aes_key": cdn_aes_key_b64,
                "encrypt_type": 1,
            },
        }

        if item_type == _ch.ITEM_IMAGE:
            media_item["mid_size"] = padded_size
        elif item_type == _ch.ITEM_VIDEO:
            media_item["video_size"] = padded_size
        elif item_type == _ch.ITEM_FILE:
            media_item["file_name"] = p.name
            media_item["len"] = str(raw_size)

        # Send each media item as its own message (matching reference plugin)
        client_id = f"erza-{uuid.uuid4().hex[:12]}"
        item_list: list[dict] = [{"type": item_type, item_key: media_item}]

        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": _ch.MESSAGE_TYPE_BOT,
            "message_state": _ch.MESSAGE_STATE_FINISH,
            "item_list": item_list,
        }
        if context_token:
            weixin_msg["context_token"] = context_token

        body: dict[str, Any] = {
            "msg": weixin_msg,
            "base_info": _ch.BASE_INFO,
        }

        data = await self._api_post("ilink/bot/sendmessage", body)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"WeChat send media error (ret={ret}, errcode={errcode}): {data.get('errmsg', '')}"
            )
