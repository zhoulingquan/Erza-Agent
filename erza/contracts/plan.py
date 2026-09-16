"""Plan contract types shared by the agent core and the tools layer.

Moved verbatim from ``erza.agent.planner`` so ``tools/activate_plan.py``
can import plan types at module level without pulling in the whole
``erza.agent`` package (which itself imports ``erza.tools``).

``erza.agent.planner`` re-exports everything below; prefer importing from
``erza.contracts.plan`` in new code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlannerStatus(str, Enum):
    """Whether the provider produced a usable managed-execution plan."""

    VALID = "valid"
    FALLBACK = "fallback"


@dataclass(slots=True)
class PlanStep:
    """One step in an execution plan."""

    id: int
    action: str
    tool_hint: str | None = None
    done_criteria: str | None = None
    status: StepStatus = StepStatus.PENDING
    failure_reason: str | None = None
    iterations_used: int = 0
    evidence_level: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "tool_hint": self.tool_hint,
            "done_criteria": self.done_criteria,
            "status": self.status.value,
            "failure_reason": self.failure_reason,
            "iterations_used": self.iterations_used,
            "evidence_level": self.evidence_level,
        }


RECEIPT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})


def _normalize_evidence_level(value: Any) -> str:
    """Fold any non-"tool" value (missing, "none", typo, non-str) onto "text".

    Never raises: the static floor still lifts write/edit/patch steps to tool
    level via their tool_hint, so a Planner that forgets to declare the field
    cannot open an acceptance hole.
    """
    return "tool" if str(value or "").strip().lower() == "tool" else "text"


def effective_evidence_level(step: PlanStep) -> str:
    """Resolve the evidence level actually enforced for a step.

    The Planner's declaration can only raise the bar, never lower it: a step
    that touches a receipt-issuing tool always demands tool-level evidence.
    """
    if step.evidence_level == "tool" or (step.tool_hint or "") in RECEIPT_TOOLS:
        return "tool"
    return "text"


@dataclass
class Plan:
    """An execution plan produced by the Planner."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    replan_count: int = 0
    max_replans: int = 3
    # StepEvidence objects (erza.agent.step_acceptance.StepEvidence); kept as
    # an untyped list here so this contract module stays independent of the
    # agent core.
    step_evidence: list = field(default_factory=list)

    @property
    def completed_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.COMPLETED]

    @property
    def failed_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.FAILED]

    @property
    def pending_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    @property
    def current_step(self) -> PlanStep | None:
        for s in self.steps:
            if s.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS):
                return s
        return None

    @property
    def all_done(self) -> bool:
        return all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED) for s in self.steps)

    @property
    def can_replan(self) -> bool:
        return self.replan_count < self.max_replans

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "replan_count": self.replan_count,
            "max_replans": self.max_replans,
        }
        if self.step_evidence and self.steps:
            data["step_evidence"] = [e.to_dict() for e in self.step_evidence]
        return data


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """Explicit planner outcome with a diagnostic fallback plan."""

    plan: Plan
    status: PlannerStatus
    error_code: str | None = None
