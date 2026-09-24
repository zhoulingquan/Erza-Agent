"""宿主机原生文件夹选择对话框(WebUI"浏览文件夹"按钮的后端直达路径)。

WebUI 与网关同机运行时(本地部署的默认形态),"浏览文件夹"按钮请求
``/api/workspaces/pick``:后端在网关进程内弹出系统原生目录选择框,把用户
选中的绝对路径返回给前端。浏览器自己拿不到本机路径(原生壳
``window.erzaHost`` 未注入时),这条后端代理是唯一通路,与 screenshot 的
本地直达路径同一设计。

无显示环境(无头 Linux / 未装 python3-tk / macOS 非主线程限制)抛
:class:`FolderPickerError`,handler 据此回复 503,前端提示改用手动粘贴
路径。
"""

from __future__ import annotations

import threading
from pathlib import Path

# tkinter 非线程安全且同一时刻只应有一个对话框;并发请求直接报"忙碌",
# 避免两个系统对话框叠在一起让用户点错。
_PICKER_LOCK = threading.Lock()

_DEFAULT_TITLE = "Select project folder"
_MAX_TITLE_CHARS = 120


class FolderPickerError(RuntimeError):
    """弹窗不可用(无 tkinter / 无显示环境 / 对话框已打开)。"""


class FolderCreateError(ValueError):
    """新建文件夹失败,附带面向前端的 HTTP 状态码。"""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def create_project_folder(parent: str | None, name: str | None) -> str:
    """在宿主机 ``parent`` 目录下新建名为 ``name`` 的文件夹,返回绝对路径。

    与系统弹窗无关,无头服务器也可调用:专供 WebUI"新建文件夹"对话框,
    是 :func:`pick_workspace_folder` 在弹不出对话框时的替代通路。
    目录名仅允许单层,防止路径穿越;父目录必须已存在(不自动建多级)。
    """
    raw_parent = (parent or "").strip()
    raw_name = (name or "").strip()
    if not raw_parent:
        raise FolderCreateError("missing parent directory", status=400)
    if not raw_name or raw_name in {".", ".."}:
        raise FolderCreateError("missing folder name", status=400)
    if "/" in raw_name or "\\" in raw_name or "\x00" in raw_name:
        raise FolderCreateError(
            "folder name must be a single directory name", status=400
        )
    base = Path(raw_parent).expanduser()
    if not base.is_absolute():
        raise FolderCreateError(
            "parent directory must be an absolute path", status=400
        )
    if not base.is_dir():
        raise FolderCreateError("parent directory does not exist", status=400)
    target = base / raw_name
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise FolderCreateError("folder already exists", status=409) from exc
    except PermissionError as exc:
        raise FolderCreateError("permission denied", status=403) from exc
    except OSError as exc:
        raise FolderCreateError(
            str(exc) or "failed to create folder", status=500
        ) from exc
    return str(target)


def pick_workspace_folder(
    start_dir: str | None = None,
    title: str | None = None,
) -> str | None:
    """弹出原生目录选择框,返回选中目录的绝对路径;用户取消返回 ``None``。

    必须在 worker 线程中调用(路由侧用 ``asyncio.to_thread`` 转发):Tk 的
    创建、事件循环、销毁必须完整发生在同一线程内,弹窗期间阻塞该线程。
    """
    if not _PICKER_LOCK.acquire(blocking=False):
        raise FolderPickerError("folder picker is already open")
    try:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as exc:
            raise FolderPickerError("tkinter is not available") from exc
        try:
            root = tk.Tk()
        except Exception as exc:  # TclError/RuntimeError:无显示环境、非主线程等
            raise FolderPickerError(str(exc)) from exc
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            root.update_idletasks()
            safe_title = (title or "").strip()[:_MAX_TITLE_CHARS] or _DEFAULT_TITLE
            chosen = filedialog.askdirectory(
                initialdir=start_dir or None,
                parent=root,
                title=safe_title,
            )
            return chosen or None
        except Exception as exc:
            raise FolderPickerError(str(exc)) from exc
        finally:
            try:
                root.destroy()
            except Exception:
                pass
    finally:
        _PICKER_LOCK.release()
