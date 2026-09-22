"""WeChat channel typing ticket / keepalive mixin.

Splits the typing-ticket cache, context-token refresh, buffered tool-hint
flush and typing keepalive senders out of ``channel.py``. Patchable names
(``time`` / ``random`` and the ``TYPING_*`` / ``CONFIG_CACHE_*``
constants) are resolved at call time through the ``channel`` module
namespace so test monkeypatches keep working.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from erza.channels.weixin import channel as _ch


class TypingMixin:
    async def _get_typing_ticket(self, user_id: str, context_token: str = "") -> str:
        """Get typing ticket with per-user refresh + failure backoff cache."""
        now = _ch.time.time()
        entry = self._typing_tickets.get(user_id)
        if entry and now < float(entry.get("next_fetch_at", 0)):
            return str(entry.get("ticket", "") or "")

        body: dict[str, Any] = {
            "ilink_user_id": user_id,
            "context_token": context_token or None,
            "base_info": _ch.BASE_INFO,
        }
        data = await self._api_post("ilink/bot/getconfig", body)
        if data.get("ret", 0) == 0:
            ticket = str(data.get("typing_ticket", "") or "")
            self._typing_tickets[user_id] = {
                "ticket": ticket,
                "ever_succeeded": True,
                "next_fetch_at": now + (_ch.random.random() * _ch.TYPING_TICKET_TTL_S),
                "retry_delay_s": _ch.CONFIG_CACHE_INITIAL_RETRY_S,
            }
            return ticket

        prev_delay = (
            float(entry.get("retry_delay_s", _ch.CONFIG_CACHE_INITIAL_RETRY_S))
            if entry
            else _ch.CONFIG_CACHE_INITIAL_RETRY_S
        )
        next_delay = min(prev_delay * 2, _ch.CONFIG_CACHE_MAX_RETRY_S)
        if entry:
            entry["next_fetch_at"] = now + next_delay
            entry["retry_delay_s"] = next_delay
            return str(entry.get("ticket", "") or "")

        self._typing_tickets[user_id] = {
            "ticket": "",
            "ever_succeeded": False,
            "next_fetch_at": now + _ch.CONFIG_CACHE_INITIAL_RETRY_S,
            "retry_delay_s": _ch.CONFIG_CACHE_INITIAL_RETRY_S,
        }
        return ""

    async def _refresh_context_token_if_stale(self, chat_id: str, context_token: str) -> str:
        """Return a fresh context_token if the cached one is too old.

        iLink context_token expires server-side after a short idle period
        (empirically ~90s). Proactively refreshing before sending prevents
        silent message loss on long agent turns or cron pushes.
        """
        if not context_token:
            return context_token

        now = _ch.time.time()
        cached_at = self._context_token_at.get(chat_id, 0)
        age = now - cached_at

        if age < _ch.CONTEXT_TOKEN_MAX_AGE_S:
            return context_token

        self.logger.debug(
            "WeChat context_token for {} is {:.0f}s old; refreshing via getconfig",
            chat_id,
            age,
        )

        body: dict[str, Any] = {
            "ilink_user_id": chat_id,
            "context_token": context_token,
            "base_info": _ch.BASE_INFO,
        }
        try:
            data = await self._api_post("ilink/bot/getconfig", body)
        except Exception as e:
            self.logger.warning("WeChat getconfig failed for {}: {}", chat_id, e)
            return context_token

        if data.get("ret", 0) != 0:
            self.logger.warning(
                "WeChat getconfig returned ret={} for {}: {}",
                data.get("ret"),
                chat_id,
                data.get("errmsg", ""),
            )
            return context_token

        new_token = str(data.get("context_token", "") or "")
        if new_token and new_token != context_token:
            self.logger.info(
                "WeChat context_token refreshed for {} (age {:.0f}s -> fresh)",
                chat_id,
                age,
            )
            self._context_tokens[chat_id] = new_token
            self._context_token_at[chat_id] = now
            self._save_state()
            return new_token

        return context_token

    async def _flush_tool_hints(self, chat_id: str) -> None:
        """Send any buffered tool hints for *chat_id* as a single message.

        Tool hints are coalesced to reduce message count and avoid hitting the
        WeChat iLink rate limit (~7 msgs / 5 min).  Failures are logged but
        not raised so that the main message send is never blocked.
        """
        hints = self._pending_tool_hints.pop(chat_id, None)
        if not hints:
            return

        self.logger.info(
            "Flushing {} buffered tool hint(s) for {}",
            len(hints),
            chat_id,
        )

        ctx_token = self._context_tokens.get(chat_id, "")
        ctx_token = await self._refresh_context_token_if_stale(chat_id, ctx_token)
        if not ctx_token:
            self.logger.warning(
                "Dropped {} buffered tool hint(s) for {}: no context_token",
                len(hints),
                chat_id,
            )
            return

        try:
            await self._send_text(chat_id, "\n\n".join(hints), ctx_token)
        except Exception:
            self.logger.exception("Failed to flush buffered tool hints for {}", chat_id)

    async def _send_typing(self, user_id: str, typing_ticket: str, status: int) -> None:
        """Best-effort sendtyping wrapper."""
        if not typing_ticket:
            return
        body: dict[str, Any] = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": _ch.BASE_INFO,
        }
        await self._api_post("ilink/bot/sendtyping", body)

    async def _typing_keepalive_loop(
        self, user_id: str, typing_ticket: str, stop_event: asyncio.Event
    ) -> None:
        try:
            while not stop_event.is_set():
                await asyncio.sleep(_ch.TYPING_KEEPALIVE_INTERVAL_S)
                if stop_event.is_set():
                    break
                with suppress(Exception):
                    await self._send_typing(user_id, typing_ticket, _ch.TYPING_STATUS_TYPING)
        finally:
            pass
