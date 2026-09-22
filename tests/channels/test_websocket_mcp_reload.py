"""回归测试：WebUI 的 MCP 热重载回调必须是「真异步且被 await」的。

历史缺陷：``WebSocketChannel._reload_mcp_safe`` 曾把 ``async`` 的
``request_mcp_reload`` 当同步函数调用：

    request_mcp_reload(self.bus)   # 协程从未被调度

结果是协程对象被直接丢弃——那条 runtime-control 消息永远不会投递到 bus，
MCP 连接不会重连，只在日志里留下 "coroutine was never awaited" 警告。
同时 ``RouteDeps.reload_mcp`` 声明为 ``Callable[[], None]``，而真实消费者
``erza/channels/websocket/api/mcp_presets_api._background_reload`` 却按 ``Awaitable[dict]``
使用，契约双向不一致。
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from erza.bus.queue import MessageBus
from erza.channels.websocket import WebSocketChannel


def _ch(bus: MessageBus, tmp_path: Path, *, mcp_reloader: Any = None) -> WebSocketChannel:
    return WebSocketChannel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 29801,
            "path": "/",
            "websocketRequiresToken": False,
        },
        bus,
        session_manager=None,
        static_dist_path=None,
        mcp_reloader=mcp_reloader,
    )


@pytest.mark.asyncio
async def test_reload_mcp_safe_really_awaits_request_mcp_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[MessageBus] = []

    async def _fake_reload(bus: MessageBus) -> dict:
        seen.append(bus)
        return {"ok": True, "message": "reloaded", "requires_restart": False}

    bus = MessageBus()
    channel = _ch(bus, tmp_path, mcp_reloader=_fake_reload)
    result = await channel._reload_mcp_safe()

    assert seen == [bus], "request_mcp_reload 没有被真正 await 执行"
    assert result == {"ok": True, "message": "reloaded", "requires_restart": False}


@pytest.mark.asyncio
async def test_reload_mcp_safe_degrades_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(_bus: MessageBus) -> dict:
        raise RuntimeError("mcp unreachable")

    channel = _ch(MessageBus(), tmp_path, mcp_reloader=_boom)
    result = await channel._reload_mcp_safe()

    assert result["ok"] is False
    assert result["requires_restart"] is True


def test_route_deps_reload_mcp_is_async(tmp_path: Path) -> None:
    """RouteDeps.reload_mcp 必须满足 McpReload = Callable[[], Awaitable[dict]]。"""
    channel = _ch(MessageBus(), tmp_path)
    deps = channel._build_route_deps()

    assert asyncio.iscoroutinefunction(deps.reload_mcp)
