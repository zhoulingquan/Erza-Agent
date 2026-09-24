"""WebSocket channel token / rate-limit / origin-allowlist mixin.

Splits the token issuance, per-IP rate limiting, Origin allowlist and the
periodic cleanup loop out of ``channel.py``.  Patchable names stay on the
``channel`` module; this mixin resolves the shared ``_RateLimiter`` /
path helpers directly from the sibling ``_ws_upgrade`` module.
"""

from __future__ import annotations

import asyncio
import secrets
import ssl
import time
from typing import Any

from ._http_routes import _http_json_response, _issue_route_secret_matches
from ._ws_upgrade import _LOCALHOSTS, _is_lan_ip, _is_localhost, _normalize_config_path, _RateLimiter


class TokenMixin:
    _MAX_ISSUED_TOKENS = 10_000
    # 流缓冲 TTL:超过该时长(秒)未更新的 buffer 视为陈旧并被清理。
    # 30 分钟覆盖正常流式回复间隔,异常中断的流不会长期占用内存。
    _STREAM_TEXT_BUF_TTL = 1800
    # 清理任务周期:每 5 分钟扫描一次,在及时回收与 CPU 开销之间取折中。
    _STREAM_TEXT_BUF_CLEANUP_INTERVAL = 300

    def _get_real_client_ip(self, connection: Any) -> str:
        """Return the real client IP, resolving X-Forwarded-For only for trusted proxies.

        When the gateway sits behind a reverse proxy, every connection's
        ``remote_address`` is the proxy's IP (often localhost).  To avoid
        trusting spoofed ``X-Forwarded-For`` headers from arbitrary clients,
        the header is only consulted when the TCP peer is in the
        ``trusted_proxies`` allowlist.
        """
        addr = getattr(connection, "remote_address", None)
        if not addr:
            return ""
        peer_ip = addr[0] if isinstance(addr, tuple) else str(addr)
        # Normalize IPv6-mapped IPv4 (``::ffff:127.0.0.1`` → ``127.0.0.1``).
        if peer_ip.startswith("::ffff:"):
            peer_ip = peer_ip[7:]

        if peer_ip in self.config.trusted_proxies:
            request = getattr(connection, "request", None)
            if request is not None:
                xff = request.headers.get("X-Forwarded-For") or request.headers.get(
                    "x-forwarded-for"
                )
                if xff:
                    # Leftmost entry is the original client IP.
                    first = xff.split(",")[0].strip()
                    if first:
                        return first
        return peer_ip

    def _is_localhost_connection(self, connection: Any) -> bool:
        """Like the module-level ``_is_localhost`` but proxy-aware."""
        ip = self._get_real_client_ip(connection)
        return ip in _LOCALHOSTS

    def _controls_allowed_connection(self, connection: Any) -> bool:
        """Return True if *connection* may use the workspace control plane.

        回环连接永远允许;内网(RFC 1918)连接仅在显式配置
        ``allow_lan_controls=true`` 时允许。公网 IP、未知对端一律拒绝。
        截图/文件夹选择/无密钥 bootstrap 等敏感面继续走严格的
        :meth:`_is_localhost_connection`,不受此开关影响。
        """
        if self._is_localhost_connection(connection):
            return True
        if not bool(getattr(self.config, "allow_lan_controls", False)):
            return False
        return _is_lan_ip(self._get_real_client_ip(connection))

    def _check_rate_limit(self, limiter: _RateLimiter, connection: Any, label: str) -> bool:
        """Return True if the request passes the rate limit, else log and return False."""
        ip = self._get_real_client_ip(connection)
        if not ip:
            return True  # Unknown peer — don't block (other auth guards apply).
        if not limiter.check(ip):
            self.logger.warning("rate limit exceeded for {} ({})", ip, label)
            return False
        return True

    def _build_origin_allowlist(self) -> set[str]:
        """构造默认 Origin 白名单:本地回环任意端口 + gateway 同源 + 管理员配置。

        默认放行 ``http``/``https`` × ``127.0.0.1``/``localhost`` × 任意端口,
        保证本地开发场景(Vite 5173、其他 dev server)始终通过;非 localhost
        来源(如局域网 IP、公网域名)仍被严格校验。若管理员在 ``allow_origin``
        中配置了额外来源,则一并加入白名单(扩展而非替换)。
        """
        hosts = ("127.0.0.1", "localhost")
        schemes = ("http", "https")
        allowlist: set[str] = set()
        for scheme in schemes:
            for host in hosts:
                # 任意端口:覆盖 dev server (5173/3000/...) 与 gateway 自身端口。
                allowlist.add(f"{scheme}://{host}")
                # 显式带端口的形式也一并放行(浏览器 Origin 通常包含端口)。
                port = int(getattr(self.config, "port", 0) or 0)
                if port > 0:
                    allowlist.add(f"{scheme}://{host}:{port}")
        # 合并管理员配置的额外来源(忽略空值与重复)。
        for origin in getattr(self.config, "allow_origin", None) or []:
            origin_norm = origin.strip()
            if origin_norm:
                allowlist.add(origin_norm)
        return allowlist

    def _is_origin_allowed(self, request: Any) -> bool:
        """校验浏览器 Origin 头是否在白名单内。

        - 空 Origin(非浏览器客户端,如 curl、httpx)→ 放行,保持向后兼容。
        - 非空 Origin → 必须严格匹配白名单(大小写敏感);否则拒绝。
        - Origin 仅取 scheme://host[:port] 部分(浏览器原生格式,无 path)。
        - localhost/127.0.0.1 任意端口默认放行(本地开发场景);其他 host 严格校验。
        """
        headers = getattr(request, "headers", None) if request is not None else None
        if headers is None:
            return True
        origin = headers.get("Origin") or headers.get("origin")
        if not origin:
            # 非浏览器请求(无 Origin 头):放行,其他认证层(secret/token/IP)继续生效。
            return True
        origin = origin.strip()
        if not origin:
            return True
        if origin in self._origin_allowlist:
            return True
        # 本地回环任意端口放行:剥离端口后再匹配一次,覆盖 dev server 动态端口。
        try:
            scheme, _, host_port = origin.partition("://")
            if scheme and host_port:
                host = host_port.split(":", 1)[0]
                if host in ("127.0.0.1", "localhost"):
                    return True
        except Exception:
            pass
        return False

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._issued_tokens.items()):
            if now > expiry:
                self._issued_tokens.pop(token_key, None)

    def _cleanup_stale_stream_text_buffers(self) -> None:
        """清理超过 ``_STREAM_TEXT_BUF_TTL`` 未更新的流文本缓冲。

        正常流式回复会在 ``stream_end`` 时主动 ``pop`` 缓冲;但若上游异常
        中断(进程崩溃/连接断开)导致 ``stream_end`` 未送达,buffer 会残留在
        ``_stream_text_buffers`` 中。此处用 TTL 兜底回收,避免内存无限增长。
        """
        cutoff = time.monotonic() - self._STREAM_TEXT_BUF_TTL
        stale = [k for k, t in self._stream_text_buffer_times.items() if t < cutoff]
        for k in stale:
            self._stream_text_buffers.pop(k, None)
            self._stream_text_buffer_times.pop(k, None)
        if stale:
            self.logger.warning(
                "清理了 {} 个陈旧的流文本缓冲(超过 {} 秒未更新)",
                len(stale),
                self._STREAM_TEXT_BUF_TTL,
            )

    def _cleanup_rate_limiters(self) -> None:
        """清理三个 RateLimiter 中窗口外已过期的 key,防止内存无限增长。

        ``_RateLimiter.cleanup()`` 会删除窗口外无 hit 的 key,但需要外部周期触发。
        连接断开后对应的 IP key 不会自动删除,长期运行会导致 ``_hits`` 字典膨胀。
        """
        self._conn_rate_limiter.cleanup()
        self._token_rate_limiter.cleanup()
        self._media_rate_limiter.cleanup()

    async def _periodic_cleanup_loop(self) -> None:
        """周期性清理后台循环。

        合并两类清理任务,避免创建多个后台 Task:
        1. 流文本缓冲 TTL 清理(周期 ``_STREAM_TEXT_BUF_CLEANUP_INTERVAL``)
        2. RateLimiter 陈旧 key 清理(共用同一周期)
        """
        while self._running:
            try:
                await asyncio.sleep(self._STREAM_TEXT_BUF_CLEANUP_INTERVAL)
                self._cleanup_stale_stream_text_buffers()
                self._cleanup_rate_limiters()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("周期清理任务异常: {}", exc)

    def _take_issued_token_if_valid(self, token_value: str | None) -> bool:
        """Validate and consume one issued token (single use per connection attempt).

        Uses single-step pop to minimize the window between lookup and removal;
        safe under asyncio's single-threaded cooperative model.
        """
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        expiry = self._issued_tokens.pop(token_value, None)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            return False
        return True

    def _handle_token_issue_http(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        elif not _is_localhost(connection):
            # 无 secret 时仅允许回环连接签发 token: 绑定 LAN IP 的部署若放行
            # 任意局域网客户端访问签发端点, 等于把 WS token 直接交给局域网内
            # 的任何主机(与 host 校验形成纵深防御)。
            self.logger.error(
                "token issuance rejected: token_issue_secret is empty and the "
                "client is not on the loopback interface"
            )
            return connection.respond(403, "Forbidden")
        else:
            self.logger.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "loopback clients can obtain connection tokens — set token_issue_secret for production."
            )
        # Per-IP token issuance rate limit (5/min).
        if not self._check_rate_limit(self._token_rate_limiter, connection, "token_issue"):
            return _http_json_response({"error": "rate limited"}, status=429)
        self._purge_expired_issued_tokens()
        if len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS:
            self.logger.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self._issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        self._issued_tokens[token_value] = time.monotonic() + float(self.config.token_ttl_s)

        return _http_json_response({"token": token_value, "expires_in": self.config.token_ttl_s})
