"""Module size budgets (pure AST-free line counts).

Single-file debt guard: no ``erza/**/*.py`` file may exceed
``MAX_FILE_LINES``. The currently-over-budget files are listed in
``SIZE_EXEMPTIONS`` as declared transitional debt — each entry names the
split direction. The test asserts exact set equality, so:

- a NEW file over budget fails (unexpected);
- shrinking an exempted file below budget fails as *stale*, forcing the
  exemption entry to be removed.

Only production code is scanned (``tests/`` grows differently:
data-driven cases, not responsibilities).
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "erza"

MAX_FILE_LINES = 1200

# Declared transitional debt: file → split direction. Remove the entry
# once the file is back under budget (the test enforces this).
SIZE_EXEMPTIONS = frozenset(
    {
        # 1537/1491: execution/ subpackage extracted; remainder is the
        # loop/runner orchestration core.
        "agent/loop.py",
        "agent/runner.py",
    }
)


def _count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def test_no_file_exceeds_size_budget() -> None:
    over: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if _count_lines(path) > MAX_FILE_LINES:
            over.add(path.relative_to(SRC_ROOT).as_posix().removesuffix(".py"))
    # Exemptions store paths without the .py suffix for readability.
    exempt = {e.removesuffix(".py") for e in SIZE_EXEMPTIONS}
    assert over == exempt, (
        f"module size budget ({MAX_FILE_LINES} lines) mismatch:\n"
        f"  unexpected (shrink or exempt): {sorted(over - exempt)}\n"
        f"  stale (already under budget, remove exemption): {sorted(exempt - over)}"
    )


# New-file ceiling: no file created after 2026-09-20 may exceed
# NEW_FILE_BUDGET. Files already above it are grandfathered below;
# the test asserts exact set equality, so slimming a grandfathered
# file below budget fails as *stale* (remove the entry), and any
# unlisted file crossing the ceiling fails as *unexpected*.
# Rationale: MAX_FILE_LINES stays 1200 for the two historic core files
# (loop/runner); 800 stops new debt.
NEW_FILE_BUDGET = 800

GRANDFATHERED_800 = frozenset(
    {
        "tools/filesystem.py",
        "cli/commands.py",
        "channels/websocket/api/mcp_presets_api.py",
        "providers/model_catalog.py",
        "tools/mcp.py",
        "session/manager.py",
        "channels/websocket/api/transcript.py",
        "memory/store.py",
        "providers/openai_compat_provider.py",
        "cron/service.py",
        "tools/shell.py",
        "providers/base.py",
        "channels/dingtalk/channel.py",
        "channels/qrcode_auth.py",
    }
)


def test_no_new_file_exceeds_800() -> None:
    over: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if _count_lines(path) > NEW_FILE_BUDGET:
            over.add(path.relative_to(SRC_ROOT).as_posix().removesuffix(".py"))
    allowed = {e.removesuffix(".py") for e in SIZE_EXEMPTIONS} | {
        e.removesuffix(".py") for e in GRANDFATHERED_800
    }
    assert over == allowed, (
        f"new-file budget ({NEW_FILE_BUDGET} lines) mismatch:\n"
        f"  unexpected (split or grandfather): {sorted(over - allowed)}\n"
        f"  stale (already under budget, remove entry): {sorted(allowed - over)}"
    )
