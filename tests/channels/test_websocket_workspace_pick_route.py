"""``/api/workspaces/pick``(宿主机原生目录选择框)的单元测试。"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from erza.channels.websocket._http_router import RouteContext, router
from erza.channels.websocket.api.folder_picker import FolderPickerError
from erza.channels.websocket.handlers import misc


def _ctx(
    *,
    localhost: bool = True,
    query: dict[str, list[str]] | None = None,
) -> RouteContext:
    deps = MagicMock()
    deps.is_localhost_connection.return_value = localhost
    return RouteContext(
        deps=deps,
        connection=object(),
        request=MagicMock(),
        query=query or {},
        got="/api/workspaces/pick",
    )


_run = asyncio.run


def test_route_is_registered_for_get_and_post() -> None:
    entry = router._exact["/api/workspaces/pick"]
    assert entry.meta.methods == frozenset({"GET", "POST"})
    assert entry.meta.public is False
    assert entry.fn is misc.pick_project_folder


def test_pick_folder_requires_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):  # pragma: no cover - localhost 拒绝在前
        raise AssertionError("picker must not run for remote connections")

    monkeypatch.setattr(misc, "pick_workspace_folder", fail)
    resp = _run(misc.pick_project_folder(_ctx(localhost=False)))
    assert resp.status_code == 403


def test_pick_folder_passes_query_params(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str | None, str | None]] = []

    async def fake_to_thread(fn, *args):
        return fn(*args)

    def fake_picker(start_dir=None, title=None):
        calls.append((start_dir, title))
        return str(tmp_path)

    monkeypatch.setattr(misc.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(misc, "pick_workspace_folder", fake_picker)
    resp = _run(
        misc.pick_project_folder(
            _ctx(query={"start_dir": ["/home/user"], "title": ["Custom title"]})
        )
    )
    assert resp.status_code == 200
    body = json.loads(resp.body.decode())
    assert body == {"picked": True, "path": str(tmp_path)}
    assert calls == [("/home/user", "Custom title")]


def test_pick_folder_cancelled_returns_picked_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_to_thread(fn, *args):
        return fn(*args)

    monkeypatch.setattr(misc.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(misc, "pick_workspace_folder", lambda start_dir=None, title=None: None)
    resp = _run(misc.pick_project_folder(_ctx()))
    assert resp.status_code == 200
    body = json.loads(resp.body.decode())
    assert body == {"picked": False, "path": None}


def test_pick_folder_headless_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_to_thread(fn, *args):
        raise FolderPickerError("tkinter is not available")

    monkeypatch.setattr(misc.asyncio, "to_thread", fake_to_thread)
    resp = _run(misc.pick_project_folder(_ctx()))
    assert resp.status_code == 503
    assert b"tkinter is not available" in resp.body


def test_pick_folder_rejects_relative_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_to_thread(fn, *args):
        return "relative/project"

    monkeypatch.setattr(misc.asyncio, "to_thread", fake_to_thread)
    resp = _run(misc.pick_project_folder(_ctx()))
    assert resp.status_code == 500


def test_folder_picker_lock_rejects_concurrent_dialogs() -> None:
    from erza.channels.websocket.api import folder_picker

    acquired = folder_picker._PICKER_LOCK.acquire(blocking=False)
    assert acquired is True
    try:
        with pytest.raises(FolderPickerError, match="already open"):
            folder_picker.pick_workspace_folder()
    finally:
        folder_picker._PICKER_LOCK.release()
