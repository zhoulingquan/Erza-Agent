"""WebSocket channel server lifecycle mixin.

Splits ``start`` / ``_connection_loop`` / ``stop`` / ``_safe_send_to`` out of
``channel.py``.  No patchable globals are resolved here; candidates compose
into ``WebSocketChannel`` through MRO.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve, unix_serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest

from ._http_routes import _parse_request_path, _query_first
from ._session import _parse_envelope, _parse_inbound_payload
from ._ws_upgrade import _normalize_config_path


class LifecycleMixin:
    async def start(self) -> None:
        from erza.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(
            connection: ServerConnection,
            request: WsRequest,
        ) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}",
            (
                f"unix:{self.config.unix_socket_path}{self.config.path}"
                if self.config.unix_socket_path
                else f"{scheme}://{self.config.host}:{self.config.port}{self.config.path}"
            ),
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}",
                (
                    f"unix:{self.config.unix_socket_path}{_normalize_config_path(self.config.token_issue_path)}"
                    if self.config.unix_socket_path
                    else (
                        f"{scheme}://{self.config.host}:{self.config.port}"
                        f"{_normalize_config_path(self.config.token_issue_path)}"
                    )
                ),
            )

        async def runner() -> None:
            socket_path = self.config.unix_socket_path
            if socket_path:
                path_obj = Path(socket_path)
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                with suppress(FileNotFoundError):
                    path_obj.unlink()
                server = await unix_serve(
                    handler,
                    socket_path,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    # process_request also serves plain HTTP API routes (e.g.
                    # /api/settings/provider/models) that may take >10s when
                    # upstream providers are slow. The default open_timeout=10
                    # would abort the request mid-handler. Disable it so the
                    # HTTP routes can run as long as they need.
                    open_timeout=None,
                )
                with suppress(OSError):
                    path_obj.chmod(0o600)
            else:
                server = await serve(
                    handler,
                    self.config.host,
                    self.config.port,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    ssl=ssl_context,
                    # See comment above: HTTP API routes need no handshake timeout.
                    open_timeout=None,
                )
            try:
                assert self._stop_event is not None
                await self._stop_event.wait()
            finally:
                server.close()
                await server.wait_closed()
                if socket_path:
                    with suppress(FileNotFoundError):
                        Path(socket_path).unlink()

        self._server_task = asyncio.create_task(runner())
        # 启动周期性清理后台任务(流缓冲 TTL + RateLimiter 陈旧 key)
        self._periodic_cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        # Per-IP connection rate limit (10/min).
        if not self._check_rate_limit(self._conn_rate_limiter, connection, "connection"):
            with suppress(Exception):
                await connection.close(code=1013, reason="rate limited")
            return

        default_chat_id = str(uuid.uuid4())

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                    },
                    ensure_ascii=False,
                )
            )
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WebSocket already authenticates at handshake time (token),
                # so sender authorization is handled there, not via allowFrom.
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                )
        except ConnectionClosed as e:
            self.logger.debug("connection ended: {}", e)
        except Exception as e:
            # 非正常断开(协议/序列化/业务处理错误)需要比 debug 更高的
            # 可见性,便于区分崩溃原因。
            self.logger.warning("connection ended with error: {}", e)
        finally:
            self._cleanup_connection(connection)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        # 取消周期性清理任务,等待其退出
        if self._periodic_cleanup_task is not None:
            self._periodic_cleanup_task.cancel()
            with suppress(Exception):
                await self._periodic_cleanup_task
            self._periodic_cleanup_task = None
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        # 取消仍存活的后台通知任务, 防止 shutdown 期间继续向已关闭连接发送。
        for task in list(self._notify_tasks):
            task.cancel()
        if self._notify_tasks:
            with suppress(Exception):
                await asyncio.gather(*self._notify_tasks, return_exceptions=True)
            self._notify_tasks.clear()
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()
        self._api_tokens.clear()
        # 清空流缓冲及其时间戳记录
        self._stream_text_buffers.clear()
        self._stream_text_buffer_times.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise
