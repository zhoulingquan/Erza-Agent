"""Skill-location contract shared by the agent core and the tools layer.

``BUILTIN_SKILLS_DIR`` moved from ``erza.agent.skills`` (which imports the
whole skills-loading stack) so ``tools/filesystem.py`` can resolve the
builtin skills directory without pulling in ``erza.agent``.

``erza.agent.skills`` re-exports it; import from ``erza.contracts.skills``
in new code.
"""

from pathlib import Path

BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"
