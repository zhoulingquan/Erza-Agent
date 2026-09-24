"""工作区控制面 LAN 放行开关(allow_lan_controls)的单元测试。

背景:主页"完全访问权限"开关灰色,是因为 can_use_full_access 仅对回环
连接为真。可信内网部署可开 allow_lan_controls,让 RFC 1918 内网已鉴权
连接也能用控制面;公网 IP 永远拒绝,截图/文件夹选择/无密钥 bootstrap
继续走严格回环判断。
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from erza.bus.queue import MessageBus  # noqa: F401 — 保持与其它 channel 测试一致的导入风格
from erza.channels.websocket import WebSocketChannel
from erza.channels.websocket._ws_upgrade import _is_lan_ip


def _conn(ip: str | None) -> Any:
    conn = SimpleNamespace()
    if ip is not None:
        conn.remote_address = (ip, 50123)
    return conn


def _channel(**kw: Any) -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 29911,
    }
    cfg.update(kw)
    return WebSocketChannel(cfg, bus)


def test_is_lan_ip_accepts_rfc1918_only() -> None:
    assert _is_lan_ip("192.168.1.134") is True
    assert _is_lan_ip("10.0.0.5") is True
    assert _is_lan_ip("172.16.0.1") is True
    assert _is_lan_ip("172.31.255.255") is True
    assert _is_lan_ip("172.15.255.255") is False
    assert _is_lan_ip("172.32.0.1") is False
    assert _is_lan_ip("8.8.8.8") is False
    assert _is_lan_ip("203.0.113.8") is False
    assert _is_lan_ip("127.0.0.1") is False
    assert _is_lan_ip("::1") is False
    assert _is_lan_ip("") is False
    assert _is_lan_ip("not-an-ip") is False


def test_controls_default_localhost_only() -> None:
    ch = _channel()
    assert ch._controls_allowed_connection(_conn("127.0.0.1")) is True
    # 开关默认关闭:内网同样拒绝
    assert ch._controls_allowed_connection(_conn("192.168.1.20")) is False
    assert ch._controls_allowed_connection(_conn("203.0.113.8")) is False
    assert ch._controls_allowed_connection(_conn(None)) is False


def test_controls_allow_lan_opens_rfc1918_not_public() -> None:
    ch = _channel(allow_lan_controls=True)
    assert ch._controls_allowed_connection(_conn("127.0.0.1")) is True
    assert ch._controls_allowed_connection(_conn("192.168.1.20")) is True
    assert ch._controls_allowed_connection(_conn("10.1.2.3")) is True
    # 公网即便开开关也拒绝;未知对端也拒绝
    assert ch._controls_allowed_connection(_conn("203.0.113.8")) is False
    assert ch._controls_allowed_connection(_conn(None)) is False


def test_route_deps_exposes_controls_allowed() -> None:
    ch = _channel(allow_lan_controls=True)
    deps = ch._build_route_deps()
    assert deps.controls_allowed(_conn("192.168.1.20")) is True
    assert deps.controls_allowed(_conn("203.0.113.8")) is False
    # 严格回环判断不受开关影响(截图/文件夹选择继续用它)
    assert ch._is_localhost_connection(_conn("192.168.1.20")) is False
    assert ch._is_localhost_connection(_conn("127.0.0.1")) is True
