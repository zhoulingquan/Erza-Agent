"""``/api/workspaces/mkdir``(宿主机新建项目文件夹)的单元测试。"""

import json
from unittest.mock import MagicMock

from erza.channels.websocket._http_router import RouteContext, router
from erza.channels.websocket.api.folder_picker import (
    FolderCreateError,
    create_project_folder,
)
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
        got="/api/workspaces/mkdir",
    )


def test_route_is_registered_for_post_only() -> None:
    entry = router._exact["/api/workspaces/mkdir"]
    assert entry.meta.methods == frozenset({"POST"})
    assert entry.meta.public is False
    assert entry.fn is misc.create_project_folder_route


def test_mkdir_requires_localhost(tmp_path) -> None:
    resp = misc.create_project_folder_route(
        _ctx(localhost=False, query={"parent": [str(tmp_path)], "name": ["x"]})
    )
    assert resp.status_code == 403
    assert not (tmp_path / "x").exists()


def test_mkdir_success(tmp_path) -> None:
    resp = misc.create_project_folder_route(
        _ctx(query={"parent": [str(tmp_path)], "name": ["proj"]})
    )
    assert resp.status_code == 200
    body = json.loads(resp.body.decode())
    assert body == {"picked": True, "path": str(tmp_path / "proj")}
    assert (tmp_path / "proj").is_dir()


def test_mkdir_missing_args_returns_400(tmp_path) -> None:
    assert (
        misc.create_project_folder_route(_ctx(query={"name": ["x"]})).status_code
        == 400
    )
    assert (
        misc.create_project_folder_route(
            _ctx(query={"parent": [str(tmp_path)]})
        ).status_code
        == 400
    )


def test_mkdir_rejects_traversal_and_relative_parent(tmp_path) -> None:
    assert (
        misc.create_project_folder_route(
            _ctx(query={"parent": [str(tmp_path)], "name": ["../evil"]})
        ).status_code
        == 400
    )
    assert (
        misc.create_project_folder_route(
            _ctx(query={"parent": ["relative/dir"], "name": ["x"]})
        ).status_code
        == 400
    )
    assert (
        misc.create_project_folder_route(
            _ctx(query={"parent": [str(tmp_path / "nope")], "name": ["x"]})
        ).status_code
        == 400
    )


def test_mkdir_existing_returns_409(tmp_path) -> None:
    (tmp_path / "dup").mkdir()
    resp = misc.create_project_folder_route(
        _ctx(query={"parent": [str(tmp_path)], "name": ["dup"]})
    )
    assert resp.status_code == 409


def test_create_project_folder_unit(tmp_path) -> None:
    assert create_project_folder(str(tmp_path), "a") == str(tmp_path / "a")
    try:
        create_project_folder(str(tmp_path), "a")
    except FolderCreateError as e:
        assert e.status == 409
    else:  # pragma: no cover
        raise AssertionError("expected FolderCreateError for existing dir")
