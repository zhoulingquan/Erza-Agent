"""Re-export shim for :mod:`erza.session.progress`.

The file-edit progress helpers were canonicalized in
``erza/session/progress.py``; this module keeps old imports working.
"""

from __future__ import annotations

from erza.session.progress import (
    TRACKED_FILE_EDIT_TOOLS,
    FileEditTracker,
    FileSnapshot,
    StreamingFileEditTracker,
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

__all__ = [
    "FileEditTracker",
    "FileSnapshot",
    "StreamingFileEditTracker",
    "TRACKED_FILE_EDIT_TOOLS",
    "build_file_edit_end_event",
    "build_file_edit_error_event",
    "build_file_edit_live_event",
    "build_file_edit_pending_event",
    "build_file_edit_start_event",
    "display_file_edit_path",
    "is_file_edit_tool",
    "line_diff_stats",
    "prepare_file_edit_tracker",
    "prepare_file_edit_trackers",
    "read_file_snapshot",
    "resolve_file_edit_path",
    "resolve_file_edit_paths",
]
