"""Backward-compatible re-export of the skills loader contract.

The ``SkillsLoader`` implementation moved to :mod:`erza.contracts.skills`
(the shared agent/transport contract layer) so the WebSocket channel's
``/api/skills*`` handlers and resource consumers no longer import
``erza.agent``. This module keeps the legacy import path working; import
from ``erza.contracts.skills`` in new code.
"""

from __future__ import annotations

from erza.contracts.skills import BUILTIN_SKILLS_DIR, SkillsLoader, is_valid_skill_name

__all__ = ["BUILTIN_SKILLS_DIR", "SkillsLoader", "is_valid_skill_name"]
