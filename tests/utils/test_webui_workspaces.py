import json

from erza.channels.websocket.api.workspaces import (
    WebUIWorkspaceController,
    read_webui_default_access_mode,
    read_webui_workspace_state,
    webui_workspace_state_path,
    workspaces_payload,
    write_webui_default_access_mode,
)
from erza.security.workspace_access import default_workspace_scope
from erza.session.manager import SessionManager


def test_workspace_state_defaults_when_file_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    state = read_webui_workspace_state()

    assert state["default_access_mode"] == "default"
    assert webui_workspace_state_path() == tmp_path / "webui" / "workspace-state.json"


def test_workspace_state_ignores_legacy_project_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    project = tmp_path / "project"
    project.mkdir()
    path = webui_workspace_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "recent_projects": [
                    {"project_path": str(project)},
                    {"project_path": str(tmp_path / "missing")},
                ],
                "last_scope": {
                    "project_path": str(project),
                    "access_mode": "full",
                },
            }
        ),
        encoding="utf-8",
    )

    state = read_webui_workspace_state()

    assert "recent_projects" not in state
    assert "last_scope" not in state
    assert state["default_access_mode"] == "default"


def test_workspace_payload_is_config_data_dir_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    default = tmp_path / "default"
    default.mkdir()

    payload = workspaces_payload(
        default_workspace=default,
        default_restrict_to_workspace=False,
        controls_available=True,
    )

    assert payload["default_scope"]["project_path"] == str(default.resolve())
    assert payload["default_scope"]["access_mode"] == "full"
    assert payload["default_access_mode"] == "default"
    assert payload["controls"]["can_change_project"] is True


def test_workspace_payload_hides_mutable_state_when_controls_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    default = tmp_path / "default"
    default.mkdir()

    payload = workspaces_payload(
        default_workspace=default,
        default_restrict_to_workspace=False,
        controls_available=False,
    )

    assert payload["default_scope"]["project_path"] == str(default.resolve())
    assert payload["controls"]["can_change_project"] is False
    assert payload["controls"]["can_use_full_access"] is False


def test_workspace_payload_uses_webui_default_access_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    default = tmp_path / "default"
    default.mkdir()

    assert write_webui_default_access_mode("full") is True
    assert write_webui_default_access_mode("full") is False

    payload = workspaces_payload(
        default_workspace=default,
        default_restrict_to_workspace=True,
        controls_available=True,
    )

    assert payload["default_access_mode"] == "full"
    assert payload["default_scope"]["project_path"] == str(default.resolve())
    assert payload["default_scope"]["access_mode"] == "full"


def test_legacy_restricted_webui_default_access_mode_maps_to_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    assert write_webui_default_access_mode("restricted") is False
    assert read_webui_default_access_mode() == "default"


def test_webui_default_access_applies_to_unscoped_old_sessions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    default = tmp_path / "default"
    default.mkdir()
    sessions = SessionManager(tmp_path / "sessions")
    sessions.save(sessions.get_or_create("websocket:old-chat"))
    write_webui_default_access_mode("full")
    controller = WebUIWorkspaceController(
        session_manager=sessions,
        default_workspace=default,
        default_restrict_to_workspace=True,
    )

    scope = controller.scope_for_session_key("websocket:old-chat")
    new_scope = controller.scope_for_new_chat({}, controls_available=True)

    assert scope.project_path == default.resolve()
    assert scope.access_mode == "full"
    assert new_scope.access_mode == "full"


def test_set_request_syncs_scope_mode_to_global_default(tmp_path, monkeypatch) -> None:
    """对话框 → 设置:本轮切 full,全局默认跟着变(单真相源)。"""
    from erza.channels.websocket.api.workspaces import read_webui_default_access_mode

    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    default = tmp_path / "default"
    project = tmp_path / "project"
    default.mkdir()
    project.mkdir()
    sessions = SessionManager(tmp_path / "sessions")
    controller = WebUIWorkspaceController(
        session_manager=sessions,
        default_workspace=default,
        default_restrict_to_workspace=True,
    )
    assert read_webui_default_access_mode() == "default"

    scope = controller.scope_for_set_request(
        {"workspace_scope": {"project_path": str(project), "access_mode": "full"}},
        chat_id="c1",
        chat_running=False,
        controls_available=True,
    )

    assert scope.access_mode == "full"
    assert read_webui_default_access_mode() == "full"


def test_set_request_project_only_change_keeps_global_default(tmp_path, monkeypatch) -> None:
    """只换项目不换权限位时,全局默认不动。"""
    from erza.channels.websocket.api.workspaces import read_webui_default_access_mode

    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    default = tmp_path / "default"
    project = tmp_path / "project"
    default.mkdir()
    project.mkdir()
    sessions = SessionManager(tmp_path / "sessions")
    controller = WebUIWorkspaceController(
        session_manager=sessions,
        default_workspace=default,
        default_restrict_to_workspace=True,
    )

    scope = controller.scope_for_set_request(
        {"workspace_scope": {"project_path": str(project), "access_mode": "restricted"}},
        chat_id="c1",
        chat_running=False,
        controls_available=True,
    )

    assert scope.project_path == project.resolve()
    assert read_webui_default_access_mode() == "default"


def test_sync_sessions_to_default_migrates_modes_keep_projects(tmp_path, monkeypatch) -> None:
    """设置 → 对话框:改默认后存盘会话权限位跟随,项目路径保留。"""
    monkeypatch.setattr("erza.channels.websocket.api.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    default = tmp_path / "default"
    project = tmp_path / "project"
    default.mkdir()
    project.mkdir()
    sessions = SessionManager(tmp_path / "sessions")
    controller = WebUIWorkspaceController(
        session_manager=sessions,
        default_workspace=default,
        default_restrict_to_workspace=True,
    )
    controller.persist_scope(
        "full-chat", default_workspace_scope(project, restrict_to_workspace=False)
    )
    controller.persist_scope(
        "restricted-chat", default_workspace_scope(project, restrict_to_workspace=True)
    )

    assert write_webui_default_access_mode("full") is True
    migrated = controller.sync_sessions_to_default_access_mode()

    assert migrated == ["restricted-chat"]
    assert controller.scope_for_session_key("websocket:restricted-chat").access_mode == "full"
    assert (
        controller.scope_for_session_key("websocket:restricted-chat").project_path
        == project.resolve()
    )
    assert controller.scope_for_session_key("websocket:full-chat").access_mode == "full"
