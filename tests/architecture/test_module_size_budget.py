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
        # 2172: extract mention parsing / markdown→card rendering /
        # media up-download / streaming-card send (pure clusters, partly
        # covered by dedicated tests already).
        "channels/feishu/channel.py",
        # 1773: token store + remaining orchestration; api/handlers/static
        # already extracted, the rest is one coherent transport class.
        "channels/websocket/channel.py",
        # 1605: same single-file channel shape as feishu.
        "channels/weixin/channel.py",
        # 1537/1491: execution/ subpackage extracted; remainder is the
        # loop/runner orchestration core.
        "agent/loop.py",
        "agent/runner.py",
        # 1436: onboarding wizard (prompts + validation + persistence).
        "cli/onboard.py",
        # 1351: per-preset handlers share one file; split by preset family.
        "channels/websocket/api/mcp_presets_api.py",
        # 1308: module helpers extracted to _openai_compat_helpers.py;
        # remainder is the provider class itself.
        "providers/openai_compat_provider.py",
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
