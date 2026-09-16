"""零散 WebUI API 端点:sessions 列表 / commands / workspaces / sidebar-state。

这类端点不属于更大的功能分组,集中在此文件。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from websockets.http11 import Response

from erza.channels.websocket.api.folder_picker import FolderPickerError, pick_workspace_folder
from erza.channels.websocket.api.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from erza.command.builtin import builtin_command_palette
from erza.session.webui_turns import websocket_turn_wall_started_at

from .._http_router import RouteContext, router
from .._http_routes import _http_error, _http_json_response, _query_first
from ._common import forbidden, require_auth, service_unavailable


@router.route("/api/sessions", methods={"GET"})
@require_auth
def list_sessions(ctx: RouteContext) -> Response:
    """列出 websocket 频道的会话(供侧边栏渲染)。"""
    if ctx.deps.session_manager is None:
        return service_unavailable("session manager unavailable")
    sessions = ctx.deps.session_manager.list_sessions()
    # Sidebar/chat listing for WS-backed sessions only — CLI / Slack / etc.
    # keys are not intended for resume over this HTTP surface.
    cleaned = []
    for s in sessions:
        key = s.get("key")
        if not (isinstance(key, str) and key.startswith("websocket:")):
            continue
        row = {k: v for k, v in s.items() if k != "path"}
        chat_id = key.split(":", 1)[1]
        started_at = websocket_turn_wall_started_at(chat_id)
        if started_at is not None:
            row["run_started_at"] = started_at
        scope = ctx.deps.webui_workspaces.scope_for_session_key(key)
        row["workspace_scope"] = scope.payload()
        cleaned.append(row)
    return _http_json_response({"sessions": cleaned})


@router.route("/api/commands", methods={"GET"})
@require_auth
def list_commands(ctx: RouteContext) -> Response:
    """返回内置斜杠命令面板。"""
    return _http_json_response({"commands": builtin_command_palette()})


@router.route("/api/workspaces", methods={"GET"})
@require_auth
def list_workspaces(ctx: RouteContext) -> Response:
    """返回工作区列表,本地连接可获取控制能力标记。"""
    return _http_json_response(
        ctx.deps.webui_workspaces.payload(
            controls_available=ctx.deps.is_localhost_connection(ctx.connection)
        )
    )


@router.route("/api/workspaces/pick", methods={"GET", "POST"})
@require_auth
async def pick_project_folder(ctx: RouteContext) -> Response:
    """弹出原生目录选择框,返回用户选中的项目文件夹(仅限本地连接)。

    供 WebUI 主页"选择项目"下拉菜单的"浏览文件夹"按钮调用:浏览器拿不到
    本机路径,由网关在本机弹系统对话框代选。无头环境 / tkinter 缺失时回
    503,前端提示改用手动粘贴路径。弹窗会阻塞 worker 线程直到用户关闭。
    """
    if not ctx.deps.is_localhost_connection(ctx.connection):
        return forbidden("folder picking is localhost-only")
    start_dir = _query_first(ctx.query, "start_dir")
    title = _query_first(ctx.query, "title")
    try:
        chosen = await asyncio.to_thread(pick_workspace_folder, start_dir, title)
    except FolderPickerError as e:
        ctx.deps.logger.warning("workspace folder picker failed: {}", e)
        return _http_error(503, str(e))
    if not chosen:
        return _http_json_response({"picked": False, "path": None})
    folder = Path(chosen).expanduser()
    if not folder.is_absolute():
        return _http_error(500, "picker returned a non-absolute path")
    return _http_json_response({"picked": True, "path": str(folder)})


@router.route("/api/webui/sidebar-state", methods={"GET"})
@require_auth
def read_sidebar_state(ctx: RouteContext) -> Response:
    """读取 WebUI 侧边栏持久化状态。"""
    return _http_json_response(read_webui_sidebar_state())


@router.route("/api/webui/sidebar-state/update", methods={"GET", "POST"})
@require_auth
def update_sidebar_state(ctx: RouteContext) -> Response:
    """更新 WebUI 侧边栏持久化状态(JSON via ``state`` query 参数)。"""
    raw_state = _query_first(ctx.query, "state")
    if raw_state is None:
        return _http_error(400, "missing state")
    try:
        decoded = json.loads(raw_state)
    except json.JSONDecodeError:
        return _http_error(400, "state must be JSON")
    if not isinstance(decoded, dict):
        return _http_error(400, "state must be an object")
    try:
        state = write_webui_sidebar_state(decoded)
    except ValueError as e:
        return _http_error(400, str(e))
    except OSError:
        ctx.deps.logger.exception("failed to write webui sidebar state")
        return _http_error(500, "failed to write sidebar state")
    return _http_json_response(state)
