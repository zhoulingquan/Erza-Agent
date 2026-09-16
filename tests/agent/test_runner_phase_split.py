"""W2-2: main-loop phase split — pure-move regression lock.

Covers ``PlanSnapshot.with_origin``, the god-method size guard on
``_run_with_ledger``, and a minimal planned-turn smoke with receipt acceptance.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.plan_snapshot import PlanSnapshot
from erza.agent.planner import Plan, PlanStep, StepStatus
from erza.agent.planning_policy import PlanningPolicy
from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from erza.tools.filesystem import WriteFileTool
from erza.tools.registry import ToolRegistry


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _make_plan() -> Plan:
    return Plan(
        goal="test goal",
        steps=[PlanStep(id=1, action="step 1"), PlanStep(id=2, action="step 2")],
        replan_count=0,
        max_replans=3,
    )


# --- 1: with_origin keeps every other field identical ------------------------


def test_with_origin_changes_only_origin() -> None:
    snapshot = PlanSnapshot.from_plan(_make_plan(), "turn-1")

    escalated = snapshot.with_origin("escalated")

    assert escalated.origin == "escalated"
    assert escalated.to_dict() == {**snapshot.to_dict(), "origin": "escalated"}
    assert escalated.digest == snapshot.digest
    # The original snapshot is untouched and immutable.
    assert snapshot.origin == "planner"
    with pytest.raises(FrozenInstanceError):
        snapshot.origin = "escalated"  # type: ignore[misc]


# --- 2: structural guard — the main loop stays an orchestrator ---------------


def test_run_with_ledger_is_not_a_god_method() -> None:
    source = inspect.getsource(AgentRunner._run_with_ledger)

    assert len(source.splitlines()) < 260


# --- 5: managed-turn smoke with receipt acceptance ---------------------------


@pytest.mark.asyncio
async def test_managed_turn_smoke_with_receipt_acceptance(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(WriteFileTool(workspace=tmp_path))
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=json.dumps(
                    {
                        "goal": "ship",
                        "steps": [
                            {"id": 1, "action": "write a.txt", "done_criteria": "written"},
                            {"id": 2, "action": "report", "done_criteria": "reported"},
                        ],
                    }
                ),
                usage={},
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "a.txt", "content": "hello"},
                    )
                ],
                usage={},
            ),
            LLMResponse(content="written", usage={}),
            LLMResponse(content="reported", usage={}),
        ]
    )

    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=tools,
            model="test-model",
            max_iterations=8,
            max_tool_result_chars=1000,
            planning_policy=PlanningPolicy(force_plan=True),
        )
    )

    assert result.stop_reason == "completed"
    assert result.final_content == "reported"
    assert result.plan is not None
    assert all(step.status is StepStatus.COMPLETED for step in result.plan.steps)
    step1_evidence = result.plan.step_evidence[0]
    assert step1_evidence.accepted is True
    assert step1_evidence.observations[0]["tool_name"] == "write_file"
    assert step1_evidence.observations[0]["receipt"]["committed"] is True
    assert [o["tool_name"] for o in result.tool_observations] == ["write_file"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"
