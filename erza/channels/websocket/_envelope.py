"""WebSocket channel inbound-envelope mixin.

Splits media persistence (``_save_envelope_media`` / ``_save_envelope_media_sync``),
envelope dispatch and the workspace-scope guard out of ``channel.py``.
``get_media_dir`` resolves at call time through the ``channel`` module
namespace so the test monkeypatch keeps working.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from erza.channels.websocket import channel as _ch
from erza.channels.websocket.api.mcp_presets_api import normalize_mcp_preset_mentions
from erza.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY, WorkspaceScopeError
from erza.session.webui_turns import websocket_turn_wall_started_at
from erza.utils.media_decode import FileSizeExceededError, save_base64_data_url

from ._media_sign import (
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
from ._session import _is_valid_chat_id


class EnvelopeMixin:
    async def _save_envelope_media(
        self,
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the first
        failure — the caller is expected to surface ``reason`` to the client
        and skip publishing so no half-formed message ever reaches the agent.
        On failure, any files already written to disk earlier in the same
        call are unlinked so partial ingress doesn't leak orphan files.
        ``reason`` is a short, stable token suitable for UI localization.

        Shape: ``list[{"data_url": str, "name"?: str | None}]``.

        Runs on a worker thread: a single attachment can be up to 40 MB of
        base64, and decoding + writing that inline would block every other
        connection and timer on the gateway's event loop.
        """
        return await asyncio.to_thread(self._save_envelope_media_sync, media)

    def _save_envelope_media_sync(
        self,
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """Blocking implementation of :meth:`_save_envelope_media`."""
        image_count = 0
        video_count = 0
        for item in media:
            mime = (
                _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            )
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED or mime in _DOCUMENT_MIME_ALLOWED:
                # Documents share the image attachment pool (client treats all
                # non-video attachments as a single 4-item pool).
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = _ch.get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning("failed to unlink partial media {}: {}", p, exc)
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            is_document = mime in _DOCUMENT_MIME_ALLOWED
            if is_video:
                max_bytes = _MAX_VIDEO_BYTES
            elif is_document:
                max_bytes = _MAX_DOCUMENT_BYTES
            else:
                max_bytes = _MAX_IMAGE_BYTES
            # Preserve the original filename so ``save_base64_data_url`` can
            # fall back to its extension when the MIME is ``application/octet-stream``
            # (browsers return this for .log/.toml/.ini/.cfg and other text
            # formats that ``extract_documents()`` parses by extension).
            name_hint = item.get("name") if isinstance(item.get("name"), str) else None
            try:
                saved = save_base64_data_url(
                    data_url,
                    media_dir,
                    max_bytes=max_bytes,
                    filename_hint=name_hint,
                )
            except FileSizeExceededError:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Route one typed inbound envelope (``new_chat`` / ``attach`` / ``message``)."""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            # Echo the client-supplied ``request_id`` so the WebUI can
            # correlate a pending ``newChat()`` promise with the correct
            # ``attached`` response. A missing/empty ``request_id`` keeps
            # the old protocol working for legacy clients.
            req_id = envelope.get("request_id")
            if not isinstance(req_id, str) or not req_id:
                req_id = None
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._webui_workspaces.scope_for_new_chat(
                    envelope,
                    controls_available=self._controls_allowed_connection(connection),
                ),
            )
            if scope is None:
                return
            self._webui_workspaces.persist_scope(new_id, scope)
            self._attach(connection, new_id)
            attached_fields: dict[str, Any] = {"chat_id": new_id}
            if req_id is not None:
                attached_fields["request_id"] = req_id
            await self._send_event(connection, "attached", **attached_fields)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=new_id,
                scope="metadata",
                workspace_scope=scope.payload(),
            )
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "set_workspace_scope":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._webui_workspaces.scope_for_set_request(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._controls_allowed_connection(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return
            self._webui_workspaces.persist_scope(cid, scope)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=cid,
                scope="metadata",
                workspace_scope=scope.payload(),
            )
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection,
                        "error",
                        detail="image_rejected",
                        reason="malformed",
                    )
                    return
                # Per-IP media upload rate limit (10/hour).
                if not self._check_rate_limit(self._media_rate_limiter, connection, "media_upload"):
                    await self._send_event(
                        connection,
                        "error",
                        detail="image_rejected",
                        reason="rate_limited",
                    )
                    return
                media_paths, reason = await self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(
                        connection,
                        "error",
                        detail="image_rejected",
                        reason=reason,
                    )
                    return

            # Allow image-only turns (content may be empty when media is attached).
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._webui_workspaces.scope_for_message(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._controls_allowed_connection(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return

            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
            mcp_presets = normalize_mcp_preset_mentions(envelope.get("mcp_presets"))
            if mcp_presets:
                metadata["mcp_presets"] = mcp_presets
            metadata[WORKSPACE_SCOPE_METADATA_KEY] = scope.metadata()
            self._webui_workspaces.persist_scope(cid, scope)
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def _workspace_scope_or_error(
        self,
        connection: Any,
        resolver: Callable[[], Any],
        *,
        chat_id: str | None = None,
    ) -> Any | None:
        try:
            return resolver()
        except WorkspaceScopeError as exc:
            await self._send_event(
                connection,
                "error",
                detail="workspace_scope_rejected",
                reason=exc.message,
                **({"chat_id": chat_id} if chat_id else {}),
            )
            return None
