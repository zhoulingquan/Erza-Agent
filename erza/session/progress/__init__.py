"""Canonical home for WebUI progress event buckets & streaming file-edit tracker.

Formerly the flat ``erza/session/progress.py`` (995 lines); split into this
package so every module stays under the 800-line new-file budget:

- ``_file_edits``: file-edit event builders + snapshot/diff helpers.
- ``_streaming``: the argument-streaming file-edit tracker.
- ``_tool_events``: tool-event payload builders + tool-hint formatting.
"""

from __future__ import annotations

from erza.session.progress._file_edits import (
    TRACKED_FILE_EDIT_TOOLS,
    FileEditTracker,
    FileSnapshot,
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_live_event,
    build_file_edit_pending_event,
    build_file_edit_start_event,
    display_file_edit_path,
    is_file_edit_tool,
    line_diff_stats,
    prepare_file_edit_tracker,
    prepare_file_edit_trackers,
    read_file_snapshot,
    resolve_file_edit_path,
    resolve_file_edit_paths,
)
from erza.session.progress._streaming import StreamingFileEditTracker
from erza.session.progress._tool_events import (
    ProgressCallback,
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    format_tool_hints,
    invoke_file_edit_progress,
    invoke_on_progress,
    on_progress_accepts_file_edit_events,
    on_progress_accepts_tool_events,
    tool_event_result_extras,
)

__all__ = [
    "FileEditTracker",
    "FileSnapshot",
    "ProgressCallback",
    "StreamingFileEditTracker",
    "TRACKED_FILE_EDIT_TOOLS",
    "build_file_edit_end_event",
    "build_file_edit_error_event",
    "build_file_edit_live_event",
    "build_file_edit_pending_event",
    "build_file_edit_start_event",
    "build_tool_event_finish_payloads",
    "build_tool_event_start_payload",
    "display_file_edit_path",
    "format_tool_hints",
    "invoke_file_edit_progress",
    "invoke_on_progress",
    "is_file_edit_tool",
    "line_diff_stats",
    "on_progress_accepts_file_edit_events",
    "on_progress_accepts_tool_events",
    "prepare_file_edit_tracker",
    "prepare_file_edit_trackers",
    "read_file_snapshot",
    "resolve_file_edit_path",
    "resolve_file_edit_paths",
    "tool_event_result_extras",
]
