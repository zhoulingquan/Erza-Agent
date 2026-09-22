"""Feishu channel streaming-buffer maintenance mixin.

Splits the background-task bookkeeping + stream-buffer TTL cleanup cluster out
of ``channel.py``.  Deliberately kept separate from ``_stream_buf.py`` (which
only hosts the ``_FeishuStreamBuf`` dataclass): these are channel-level methods
that mutate ``self._stream_bufs`` / ``_reaction_ids`` and belong to the
``FeishuChannel`` composition, not to the buffer value type.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any


class StreamMaintenanceMixin:
    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Callback: remove from tracking set and log unhandled exceptions."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.logger.warning("Background task failed: {}", exc)

    async def _cleanup_stale_stream_bufs(self) -> None:
        """周期性清理超过 ``_STREAM_BUF_TTL`` 未更新的流缓冲。

        正常流式回复会在 ``stream_end`` 时主动 ``pop`` 缓冲;但若上游异常
        中断(进程崩溃/网络断开)导致 ``stream_end`` 未送达,buf 会残留在
        ``_stream_bufs`` 中。此处用 TTL 兜底回收,避免内存无限增长。
        """
        cutoff = time.monotonic() - self._STREAM_BUF_TTL
        stale = [k for k, v in self._stream_bufs.items() if v.last_update < cutoff]
        for k in stale:
            self._stream_bufs.pop(k, None)
        if stale:
            self.logger.warning(
                "清理了 {} 个陈旧的流缓冲(超过 {} 秒未更新)",
                len(stale),
                self._STREAM_BUF_TTL,
            )

    async def _stream_buf_cleanup_loop(self) -> None:
        """流缓冲 TTL 清理后台循环,周期由 ``_STREAM_BUF_CLEANUP_INTERVAL`` 控制。"""
        while self._running:
            try:
                await asyncio.sleep(self._STREAM_BUF_CLEANUP_INTERVAL)
                await self._cleanup_stale_stream_bufs()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("流缓冲清理任务异常: {}", exc)

    def _on_reaction_added(self, message_id: str, task: asyncio.Task) -> None:
        """Callback: store reaction_id after background add-reaction completes."""
        if task.cancelled():
            return
        # Failures already logged by _on_background_task_done.
        with suppress(Exception):
            reaction_id = task.result()
            if reaction_id:
                self._reaction_ids[message_id] = reaction_id
        # Trim cache to prevent unbounded growth
        if len(self._reaction_ids) > 500:
            self._reaction_ids.pop(next(iter(self._reaction_ids)))

    @staticmethod
    def _stream_key(chat_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Scope streaming buffers to the inbound message when available."""
        meta = metadata or {}
        return meta.get("message_id") or chat_id
