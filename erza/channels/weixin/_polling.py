"""WeChat channel polling mixin.

Splits the long-poll loop and inbound message processing out of
``channel.py`` (``_poll_once`` / ``_process_message`` plus the session
pause helpers). Patchable names (``time``, module constants) are resolved
at call time through the ``channel`` module namespace so test
monkeypatches keep working.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from erza.channels.weixin import channel as _ch


class PollMixin:
    def _pause_session(self, duration_s: int = 3600) -> None:
        # Default mirrors channel.SESSION_PAUSE_DURATION_S (60 * 60), evaluated
        # at definition time (call-time lookups cannot seed defaults).
        self._session_pause_until = _ch.time.time() + duration_s

    def _session_pause_remaining_s(self) -> int:
        remaining = int(self._session_pause_until - _ch.time.time())
        if remaining <= 0:
            self._session_pause_until = 0.0
            return 0
        return remaining

    def _assert_session_active(self) -> None:
        remaining = self._session_pause_remaining_s()
        if remaining > 0:
            remaining_min = max((remaining + 59) // 60, 1)
            raise RuntimeError(
                f"WeChat session paused, {remaining_min} min remaining (errcode {_ch.ERRCODE_SESSION_EXPIRED})"
            )

    async def _poll_once(self) -> None:
        remaining = self._session_pause_remaining_s()
        if remaining > 0:
            await asyncio.sleep(remaining)
            return

        body: dict[str, Any] = {
            "get_updates_buf": self._get_updates_buf,
            "base_info": _ch.BASE_INFO,
        }

        # Adjust httpx timeout to match the current poll timeout
        assert self._client is not None
        self._client.timeout = httpx.Timeout(self._next_poll_timeout_s + 10, connect=30)

        data = await self._api_post("ilink/bot/getupdates", body)

        # Check for API-level errors (monitor.ts checks both ret and errcode)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)

        is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)

        if is_error:
            if errcode == _ch.ERRCODE_SESSION_EXPIRED or ret == _ch.ERRCODE_SESSION_EXPIRED:
                self._pause_session()
                remaining = self._session_pause_remaining_s()
                self.logger.warning(
                    "session expired (errcode {}). Pausing {} min.",
                    errcode,
                    max((remaining + 59) // 60, 1),
                )
                return
            raise RuntimeError(
                f"getUpdates failed: ret={ret} errcode={errcode} errmsg={data.get('errmsg', '')}"
            )

        # Honour server-suggested poll timeout (monitor.ts:102-105)
        server_timeout_ms = data.get("longpolling_timeout_ms")
        if server_timeout_ms and server_timeout_ms > 0:
            self._next_poll_timeout_s = max(server_timeout_ms // 1000, 5)

        # Update cursor
        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
            self._save_state()

        # Process messages (WeixinMessage[] from types.ts)
        msgs: list[dict] = data.get("msgs", []) or []
        for msg in msgs:
            try:
                await self._process_message(msg)
            except Exception:
                self.logger.exception("Failed to process WeChat message")

    async def _process_message(self, msg: dict) -> None:
        """Process a single WeixinMessage from getUpdates."""
        # Skip bot's own messages (message_type 2 = BOT)
        if msg.get("message_type") == _ch.MESSAGE_TYPE_BOT:
            return

        msg_id = str(msg.get("message_id", "") or msg.get("seq", ""))
        if not msg_id:
            msg_id = f"{msg.get('from_user_id', '')}_{msg.get('create_time_ms', '')}"

        from_user_id = msg.get("from_user_id", "") or ""
        if not from_user_id:
            return

        # Deduplication by message_id
        if not self._dedup_message(msg_id):
            return

        ctx_token = msg.get("context_token", "")
        if not self.is_allowed(from_user_id):
            # Access is managed exclusively via allowFrom; unauthorized
            # senders are denied without side effects (no reply, no token
            # caching — replying would require the context_token anyway).
            self.logger.warning(
                "Access denied for sender {}. "
                "Add them to allowFrom list in config to grant access.",
                from_user_id,
            )
            return

        # Cache context_token (required for all replies — inbound.ts:23-27)
        if ctx_token:
            self._context_tokens[from_user_id] = ctx_token
            self._context_token_at[from_user_id] = _ch.time.time()
            self._save_state()

        # Parse item_list (WeixinMessage.item_list — types.ts:161)
        item_list: list[dict] = msg.get("item_list") or []
        content_parts: list[str] = []
        media_paths: list[str] = []
        has_top_level_downloadable_media = False

        for item in item_list:
            item_type = item.get("type", 0)

            if item_type == _ch.ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                if text:
                    # Handle quoted/ref messages (inbound.ts:86-98)
                    ref = item.get("ref_msg")
                    if ref:
                        ref_item = ref.get("message_item")
                        # If quoted message is media, just pass the text
                        if ref_item and ref_item.get("type", 0) in (
                            _ch.ITEM_IMAGE,
                            _ch.ITEM_VOICE,
                            _ch.ITEM_FILE,
                            _ch.ITEM_VIDEO,
                        ):
                            content_parts.append(text)
                        else:
                            parts: list[str] = []
                            if ref.get("title"):
                                parts.append(ref["title"])
                            if ref_item:
                                ref_text = (ref_item.get("text_item") or {}).get("text", "")
                                if ref_text:
                                    parts.append(ref_text)
                            if parts:
                                content_parts.append(f"[引用: {' | '.join(parts)}]\n{text}")
                            else:
                                content_parts.append(text)
                    else:
                        content_parts.append(text)

            elif item_type == _ch.ITEM_IMAGE:
                image_item = item.get("image_item") or {}
                has_locator = _ch._has_downloadable_media_locator(image_item.get("media"))
                if has_locator:
                    has_top_level_downloadable_media = True
                file_path = await self._download_media_item(image_item, "image")
                if file_path:
                    content_parts.append(f"[image]\n[Image: source: {file_path}]")
                    media_paths.append(file_path)
                elif has_locator:
                    # 有下载源但下载失败:向 LLM 明示,避免其误以为没有附件
                    content_parts.append("[image (download failed)]")
                else:
                    content_parts.append("[image]")

            elif item_type == _ch.ITEM_VOICE:
                voice_item = item.get("voice_item") or {}
                # Voice-to-text provided by WeChat (inbound.ts:101-103)
                voice_text = voice_item.get("text", "")
                if voice_text:
                    content_parts.append(f"[voice] {voice_text}")
                else:
                    has_locator = _ch._has_downloadable_media_locator(
                        voice_item.get("media")
                    )
                    if has_locator:
                        has_top_level_downloadable_media = True
                    file_path = await self._download_media_item(voice_item, "voice")
                    if file_path:
                        transcription = await self.transcribe_audio(file_path)
                        if transcription:
                            content_parts.append(f"[voice] {transcription}")
                        else:
                            content_parts.append(f"[voice]\n[Audio: source: {file_path}]")
                        media_paths.append(file_path)
                    elif has_locator:
                        content_parts.append("[voice (download failed)]")
                    else:
                        content_parts.append("[voice]")

            elif item_type == _ch.ITEM_FILE:
                file_item = item.get("file_item") or {}
                has_locator = _ch._has_downloadable_media_locator(file_item.get("media"))
                if has_locator:
                    has_top_level_downloadable_media = True
                file_name = file_item.get("file_name", "unknown")
                file_path = await self._download_media_item(
                    file_item,
                    "file",
                    file_name,
                )
                if file_path:
                    content_parts.append(f"[file: {file_name}]\n[File: source: {file_path}]")
                    media_paths.append(file_path)
                elif has_locator:
                    content_parts.append(f"[file: {file_name} (download failed)]")
                else:
                    content_parts.append(f"[file: {file_name}]")

            elif item_type == _ch.ITEM_VIDEO:
                video_item = item.get("video_item") or {}
                has_locator = _ch._has_downloadable_media_locator(video_item.get("media"))
                if has_locator:
                    has_top_level_downloadable_media = True
                file_path = await self._download_media_item(video_item, "video")
                if file_path:
                    content_parts.append(f"[video]\n[Video: source: {file_path}]")
                    media_paths.append(file_path)
                elif has_locator:
                    content_parts.append("[video (download failed)]")
                else:
                    content_parts.append("[video]")

        # Fallback: when no top-level media was downloaded, try quoted/referenced media.
        # This aligns with the reference plugin behavior that checks ref_msg.message_item
        # when main item_list has no downloadable media.
        if not media_paths and not has_top_level_downloadable_media:
            ref_media_item: dict[str, Any] | None = None
            for item in item_list:
                if item.get("type", 0) != _ch.ITEM_TEXT:
                    continue
                ref = item.get("ref_msg") or {}
                candidate = ref.get("message_item") or {}
                if candidate.get("type", 0) in (
                    _ch.ITEM_IMAGE,
                    _ch.ITEM_VOICE,
                    _ch.ITEM_FILE,
                    _ch.ITEM_VIDEO,
                ):
                    ref_media_item = candidate
                    break

            if ref_media_item:
                ref_type = ref_media_item.get("type", 0)
                if ref_type == _ch.ITEM_IMAGE:
                    image_item = ref_media_item.get("image_item") or {}
                    file_path = await self._download_media_item(image_item, "image")
                    if file_path:
                        content_parts.append(f"[image]\n[Image: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == _ch.ITEM_VOICE:
                    voice_item = ref_media_item.get("voice_item") or {}
                    file_path = await self._download_media_item(voice_item, "voice")
                    if file_path:
                        transcription = await self.transcribe_audio(file_path)
                        if transcription:
                            content_parts.append(f"[voice] {transcription}")
                        else:
                            content_parts.append(f"[voice]\n[Audio: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == _ch.ITEM_FILE:
                    file_item = ref_media_item.get("file_item") or {}
                    file_name = file_item.get("file_name", "unknown")
                    file_path = await self._download_media_item(file_item, "file", file_name)
                    if file_path:
                        content_parts.append(f"[file: {file_name}]\n[File: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == _ch.ITEM_VIDEO:
                    video_item = ref_media_item.get("video_item") or {}
                    file_path = await self._download_media_item(video_item, "video")
                    if file_path:
                        content_parts.append(f"[video]\n[Video: source: {file_path}]")
                        media_paths.append(file_path)

        content = "\n".join(content_parts)
        if not content:
            return

        self.logger.info(
            "inbound: from={} items={} bodyLen={}",
            from_user_id,
            ",".join(str(i.get("type", 0)) for i in item_list),
            len(content),
        )

        await self._start_typing(from_user_id, ctx_token)

        await self._handle_message(
            sender_id=from_user_id,
            chat_id=from_user_id,
            content=content,
            media=media_paths or None,
            metadata={"message_id": msg_id},
        )
