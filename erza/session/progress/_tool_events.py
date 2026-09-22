"""Tool-event & tool-hint progress helpers.

Structured progress-event payload building and concise tool-call hint
formatting, canonical home of both.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any

from erza.contracts.callbacks import ProgressCallback
from erza.utils.path import abbreviate_path

__all__ = [
    "ProgressCallback",
    "build_tool_event_finish_payloads",
    "build_tool_event_start_payload",
    "format_tool_hints",
    "invoke_file_edit_progress",
    "invoke_on_progress",
    "on_progress_accepts_file_edit_events",
    "on_progress_accepts_tool_events",
    "tool_event_result_extras",
]


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "tool_events")


def on_progress_accepts_file_edit_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "file_edit_events")


def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


async def invoke_on_progress(
    on_progress: ProgressCallback,
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
) -> None:
    if tool_events and on_progress_accepts_tool_events(on_progress):
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


async def invoke_file_edit_progress(
    on_progress: ProgressCallback,
    file_edit_events: list[dict[str, Any]],
) -> None:
    if not file_edit_events or not on_progress_accepts_file_edit_events(on_progress):
        return
    await on_progress("", file_edit_events=file_edit_events)


def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": getattr(tool_call, "name", ""),
        "arguments": getattr(tool_call, "arguments", {}) or {},
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    embeds = result.get("embeds") if isinstance(result.get("embeds"), list) else []
    return files, embeds


def build_tool_event_finish_payloads(
    tool_calls: list[Any], tool_results: list[Any], tool_events: list[Any]
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(tool_calls), len(tool_results), len(tool_events))
    for idx in range(count):
        tool_call = tool_calls[idx]
        result = tool_results[idx]
        event = tool_events[idx] if isinstance(tool_events[idx], dict) else {}
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": getattr(tool_call, "arguments", {}) or {},
            "result": result if phase == "end" else None,
            "error": None,
            "files": files,
            "embeds": embeds,
        }
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        payloads.append(payload)
    return payloads


# Tool hint formatting for concise, human-readable tool call display.

# Registry: tool_name -> (key_args, template, is_path, is_command)
_TOOL_FORMATS: dict[str, tuple[list[str], str, bool, bool]] = {
    "read_file": (["path", "file_path"], "read {}", True, False),
    "write_file": (["path", "file_path"], "write {}", True, False),
    "edit": (["file_path", "path"], "edit {}", True, False),
    "find_files": (["query", "glob", "path"], "find {}", False, False),
    "grep": (["pattern"], 'grep "{}"', False, False),
    "exec": (["command"], "$ {}", False, True),
    "list_exec_sessions": ([], "exec sessions", False, False),
    "web_fetch": (["url"], "fetch {}", True, False),
    "list_dir": (["path"], "ls {}", True, False),
}

# Matches file paths embedded in shell commands, including quoted paths with spaces.
_PATH_IN_CMD_RE = re.compile(
    r'"(?P<double>(?:[A-Za-z]:[/\\]|~/|/)[^"]+)"'
    r"|'(?P<single>(?:[A-Za-z]:[/\\]|~/|/)[^']+)'"
    r"|(?P<bare>(?:[A-Za-z]:[/\\]|~/|(?<=\s)/)[^\s;&|<>\"']+)"
)


def format_tool_hints(tool_calls: list, max_length: int = 40) -> str:
    """Format tool calls as concise hints with smart abbreviation."""
    if not tool_calls:
        return ""

    formatted = []
    for tc in tool_calls:
        fmt = _TOOL_FORMATS.get(tc.name)
        if fmt:
            formatted.append(_fmt_known(tc, fmt, max_length))
        elif tc.name.startswith("mcp_"):
            formatted.append(_fmt_mcp(tc, max_length))
        else:
            formatted.append(_fmt_fallback(tc, max_length))

    hints = []
    for hint in formatted:
        if hints and hints[-1][0] == hint:
            hints[-1] = (hint, hints[-1][1] + 1)
        else:
            hints.append((hint, 1))

    return ", ".join(f"{h} \u00d7 {c}" if c > 1 else h for h, c in hints)


def _get_args(tc) -> dict:
    """Extract args dict from tc.arguments, handling list/dict/None/empty."""
    if tc.arguments is None:
        return {}
    if isinstance(tc.arguments, list):
        return tc.arguments[0] if tc.arguments else {}
    if isinstance(tc.arguments, dict):
        return tc.arguments
    return {}


def _extract_arg(tc, key_args: list[str]) -> str | None:
    """Extract the first available value from preferred key names."""
    args = _get_args(tc)
    if not isinstance(args, dict):
        return None
    for key in key_args:
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    for val in args.values():
        if isinstance(val, str) and val:
            return val
    return None


def _fmt_known(tc, fmt: tuple, max_length: int = 40) -> str:
    """Format a registered tool using its template."""
    if not fmt[0] and "{}" not in fmt[1]:
        return fmt[1]
    val = _extract_arg(tc, fmt[0])
    if val is None:
        return tc.name
    # The template's literal text (the "ls " / "$ " prefix) is part of the
    # rendered hint, so the payload budget must exclude it — otherwise the
    # rendered hint silently exceeds ``max_length`` (e.g. `ls <40-char path>`
    # came out 43 chars wide no matter what the caller asked for).
    overhead = len(fmt[1]) - 2 if "{}" in fmt[1] else len(fmt[1])
    budget = max(8, max_length - overhead)
    if fmt[2]:  # is_path
        val = abbreviate_path(val, max_len=budget)
    elif fmt[3]:  # is_command
        val = _abbreviate_command(val, max_len=budget)
    return fmt[1].format(val)


def _fold_paths_in_command(cmd: str, path_max: int) -> str:
    """Replace every path found in *cmd* with its folded form at *path_max*."""

    def _replace_path(match: re.Match[str]) -> str:
        if match.group("double") is not None:
            return f'"{abbreviate_path(match.group("double"), max_len=path_max)}"'
        if match.group("single") is not None:
            return f"'{abbreviate_path(match.group('single'), max_len=path_max)}'"
        return abbreviate_path(match.group("bare"), max_len=path_max)

    return _PATH_IN_CMD_RE.sub(_replace_path, cmd)


def _abbreviate_command(cmd: str, max_len: int = 40) -> str:
    """Fold the paths inside *cmd*, then make sure the result fits *max_len*.

    The per-path budget is *searched* rather than fixed at ``max_len // 2``:
    a command that overflows only because of an embedded path should have that
    path folded (``cd …/Erza/workspace && pytest tests/``) instead of being
    blindly truncated mid-path, which used to hide the command tail
    (``npm test``, flags, …).
    """
    if max_len <= 0:
        return cmd
    if len(cmd) <= max_len:
        return cmd
    if not _PATH_IN_CMD_RE.search(cmd):
        return cmd[: max_len - 1] + "\u2026" if max_len > 1 else cmd[:max_len]

    floor = 6
    for path_max in range(max(floor, max_len // 2), floor - 1, -1):
        candidate = _fold_paths_in_command(cmd, path_max)
        if len(candidate) <= max_len:
            return candidate
    folded = _fold_paths_in_command(cmd, floor)
    if len(folded) <= max_len:
        return folded
    return folded[: max_len - 1] + "\u2026" if max_len > 1 else folded[:max_len]


def _fmt_mcp(tc, max_length: int = 40) -> str:
    """Format MCP tool as server::tool."""
    name = tc.name
    if "__" in name:
        parts = name.split("__", 1)
        server = parts[0].removeprefix("mcp_")
        tool = parts[1]
    else:
        rest = name.removeprefix("mcp_")
        parts = rest.split("_", 1)
        server = parts[0] if parts else rest
        tool = parts[1] if len(parts) > 1 else ""
    if not tool:
        return name
    args = _get_args(tc)
    val = next((v for v in args.values() if isinstance(v, str) and v), None)
    if val is None:
        return f"{server}::{tool}"
    return f'{server}::{tool}("{abbreviate_path(val, max_length)}")'


def _fmt_fallback(tc, max_length: int = 40) -> str:
    """Original formatting logic for unregistered tools."""
    args = _get_args(tc)
    val = next(iter(args.values()), None) if isinstance(args, dict) else None
    if not isinstance(val, str):
        return tc.name
    return (
        f'{tc.name}("{abbreviate_path(val, max_length)}")'
        if len(val) > max_length
        else f'{tc.name}("{val}")'
    )
