"""Cross-layer contract types shared by the agent core and the tools layer.

This package is the dependency-direction firewall between ``erza.agent`` and
``erza.tools``: both sides import *from* here, and this package imports
nothing from either. Symbols that tools need at runtime (Plan dataclasses,
subagent definitions/status, the subagent recursion-depth contextvar) live
here so the module-level import cycle
``agent/__init__ → agent.loop → tools.* → agent.<submodule>`` is broken
without any late/delayed imports.

Rules for this package:
- stdlib + third-party light deps (``yaml``, ``loguru``) only.
- Never import ``erza.agent.*`` or ``erza.tools.*`` here (module level or
  function level). Types may reference collaborators only via ``Any`` or
  TYPE_CHECKING strings.
- The legacy modules (``erza.agent.planner``, ``erza.agent.subagent``,
  ``erza.agent.subagent_registry``) re-export these symbols so existing
  ``from erza.agent.planner import Plan`` call sites keep working.
"""

from erza.contracts.plan import (
    RECEIPT_TOOLS,
    Plan,
    PlannerResult,
    PlannerStatus,
    PlanStep,
    StepStatus,
    _normalize_evidence_level,
    effective_evidence_level,
)
from erza.contracts.subagent import (
    SubagentDefinition,
    SubagentRegistry,
    SubagentStatus,
    bind_subagent_depth,
    current_depth_token,
    get_current_subagent_depth,
    reset_subagent_depth,
)

__all__ = [
    "RECEIPT_TOOLS",
    "Plan",
    "PlanStep",
    "PlannerResult",
    "PlannerStatus",
    "StepStatus",
    "SubagentDefinition",
    "SubagentRegistry",
    "SubagentStatus",
    "_normalize_evidence_level",
    "bind_subagent_depth",
    "current_depth_token",
    "effective_evidence_level",
    "get_current_subagent_depth",
    "reset_subagent_depth",
]
