"""Plan-and-Execute for AgentRunner.

When the deterministic PlanningPolicy router decides a turn warrants a plan,
the runner first asks the LLM (the execution model, single-model design) to
decompose the task into ordered steps (a Plan), then executes each step using
the normal ReAct tool loop. Failed steps trigger a replan with the remaining
steps, carrying the failure reason forward.

This module is self-contained: it does not modify the existing ReAct loop
in runner.py. The Planner class is called by run() only when the planning
router says so; otherwise the plain loop runs unchanged.

The plan contract types (``Plan``/``PlanStep``/``StepStatus``/…) live in
:mod:`erza.contracts.plan` so the tools layer can import them without
pulling in the whole agent core; this module re-exports them for
backward compatibility.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from loguru import logger

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
from erza.ledger import CallPurpose, call_purpose
from erza.utils.prompt_templates import render_template

__all__ = [
    "RECEIPT_TOOLS",
    "Plan",
    "PlanStep",
    "Planner",
    "PlannerResult",
    "PlannerStatus",
    "StepStatus",
    "_normalize_evidence_level",
    "effective_evidence_level",
]


class Planner:
    """Produces and updates Plans via LLM calls.

    The Planner does NOT execute steps — that's the Executor's job (which
    reuses AgentRunner's existing ReAct loop). Planner only generates the
    plan and handles replanning on failure.
    """

    def __init__(self, provider: Any, model: str):
        self.provider = provider
        self.model = model

    async def create_plan(self, task: str, tools_summary: str) -> PlannerResult:
        """Ask the LLM to decompose *task* into a structured Plan."""
        try:
            async with call_purpose(CallPurpose.PLANNER):
                response = await self.provider.chat_with_retry(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": render_template("agent/planner_system.md", strip=True),
                        },
                        {
                            "role": "user",
                            "content": f"## Task\n{task}\n\n## Available Tools\n{tools_summary}",
                        },
                    ],
                    tools=None,
                    tool_choice=None,
                )
            if response.finish_reason == "error":
                return PlannerResult(
                    plan=self._fallback_plan(task),
                    status=PlannerStatus.FALLBACK,
                    error_code="provider_error",
                )
            return self._parse_plan_response(response.content or "", task)
        except Exception:
            logger.exception("Planner.create_plan failed; falling back to single-step plan")
            return PlannerResult(
                plan=self._fallback_plan(task),
                status=PlannerStatus.FALLBACK,
                error_code="provider_error",
            )

    async def replan(
        self,
        plan: Plan,
        failed_step: PlanStep,
        failure_reason: str,
        task: str,
        tools_summary: str,
    ) -> PlannerResult:
        """Generate a new plan for remaining work, given a failed step."""
        if not plan.can_replan:
            logger.warning(
                "Planner.replan: max_replans ({}) reached; aborting",
                plan.max_replans,
            )
            return PlannerResult(
                plan=plan,
                status=PlannerStatus.FALLBACK,
                error_code="replan_limit",
            )

        completed_summary = (
            "\n".join(f"- Step {s.id} (DONE): {s.action}" for s in plan.completed_steps) or "(none)"
        )
        try:
            plan.replan_count += 1
            async with call_purpose(CallPurpose.REPLAN):
                response = await self.provider.chat_with_retry(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": render_template("agent/planner_replan.md", strip=True),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"## Original Task\n{task}\n\n"
                                f"## Original Goal\n{plan.goal}\n\n"
                                f"## Completed Steps\n{completed_summary}\n\n"
                                f"## Failed Step\n- Step {failed_step.id}: {failed_step.action}\n"
                                f"  Failure reason: {failure_reason}\n\n"
                                f"## Available Tools\n{tools_summary}\n\n"
                                f"## Remaining Steps to Replan\n"
                                f"Produce a new plan for the remaining work, avoiding the failed approach."
                            ),
                        },
                    ],
                    tools=None,
                    tool_choice=None,
                )
            if response.finish_reason == "error":
                return PlannerResult(
                    plan=plan,
                    status=PlannerStatus.FALLBACK,
                    error_code="provider_error",
                )
            result = self._parse_plan_response(response.content or "", task)
            new_plan = result.plan
            new_plan.replan_count = plan.replan_count
            new_plan.max_replans = plan.max_replans
            if result.status is PlannerStatus.VALID:
                history = [replace(step) for step in plan.completed_steps]
                new_plan.steps = [*history, *new_plan.steps]
            return PlannerResult(
                plan=new_plan,
                status=result.status,
                error_code=result.error_code,
            )
        except Exception:
            logger.exception("Planner.replan failed; keeping existing plan")
            return PlannerResult(
                plan=plan,
                status=PlannerStatus.FALLBACK,
                error_code="provider_error",
            )

    def _parse_plan_response(self, content: str, fallback_goal: str) -> PlannerResult:
        """Extract a Plan from LLM output. Tolerates markdown code fences."""
        # Strip ```json ... ``` fences if present
        json_text = self._extract_json_block(content)
        if not json_text:
            logger.warning("Planner: no JSON found in response; using single-step fallback")
            return PlannerResult(
                plan=self._fallback_plan(fallback_goal),
                status=PlannerStatus.FALLBACK,
                error_code="missing_json",
            )

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            logger.warning("Planner: JSON parse failed; using single-step fallback")
            return PlannerResult(
                plan=self._fallback_plan(fallback_goal),
                status=PlannerStatus.FALLBACK,
                error_code="invalid_json",
            )

        if not isinstance(data, dict):
            return PlannerResult(
                plan=self._fallback_plan(fallback_goal),
                status=PlannerStatus.FALLBACK,
                error_code="missing_steps",
            )
        goal = data.get("goal", fallback_goal)
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return PlannerResult(
                plan=self._fallback_plan(goal),
                status=PlannerStatus.FALLBACK,
                error_code="missing_steps",
            )

        steps: list[PlanStep] = []
        next_id = 1
        for raw in raw_steps:
            if not isinstance(raw, dict):
                continue
            action = raw.get("action") or raw.get("description") or ""
            if not action:
                continue
            steps.append(
                PlanStep(
                    id=raw.get("id", next_id),
                    action=action,
                    tool_hint=raw.get("tool_hint"),
                    done_criteria=raw.get("done_criteria"),
                    evidence_level=_normalize_evidence_level(raw.get("evidence_level")),
                )
            )
            next_id += 1
        if not steps:
            return PlannerResult(
                plan=self._fallback_plan(goal),
                status=PlannerStatus.FALLBACK,
                error_code="all_invalid_steps",
            )
        return PlannerResult(
            plan=Plan(goal=goal, steps=steps),
            status=PlannerStatus.VALID,
        )

    @staticmethod
    def _fallback_plan(goal: str) -> Plan:
        return Plan(goal=goal, steps=[PlanStep(id=1, action=goal)])

    @staticmethod
    def _extract_json_block(text: str) -> str | None:
        """Extract a JSON object from text, tolerating markdown fences."""
        # Try fenced ```json ... ``` first
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            return m.group(1)
        # Try bare JSON object
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return m.group(0)
        return None
