"""W11-4: L3b drift escalation — unplanned turns with >=8 receipt-tool calls
escalate to a mid-turn plan.

Covers: drift trigger (D1), below-threshold no-op (D2), non-receipt tools not
counted (D3), cross-iteration accumulation (D4), planned turns never escalate
(D5), no double escalation after drift (D6), stall path unchanged (D7), and
routing zero regression (D8).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.planning_policy import PlanningPolicy
from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.providers.base import LLMResponse, ToolCallRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PLAN_JSON = json.dumps(
    {"goal": "drifted goal", "steps": [{"id": 1, "action": "drifted step"}]}
)

_GRAY_TASK = "写个注释说明这段代码"


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _kind(messages: list[dict[str, Any]]) -> str:
    """Classify a chat_with_retry call: 'router', 'planner', or 'main'."""
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content") or ""
            if "task router" in content:
                return "router"
            if "task planner" in content:
                return "planner"
            break
    return "main"


def _write(i: int) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=f"w{i}", name="write_file", arguments={"path": "a.py"})],
        usage={},
    )


def _nonreceipt(name: str, i: int) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=f"{name}{i}", name=name, arguments={})],
        usage={},
    )


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], usage={})


class _FakeProvider:
    """Provider with an explicit main-loop response queue.

    Planner calls auto-return valid plan JSON; router calls return DIRECT
    (unless overridden to return PLAN); main-loop calls consume ``responses``
    (or return a default 'done').
    """

    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        *,
        router_verdict: str = "DIRECT",
    ):
        self._responses = list(responses) if responses else []
        self._idx = 0
        self.call_log: list[str] = []
        self.router_verdict = router_verdict
        self.chat_with_retry = AsyncMock(side_effect=self._dispatch)
        self.get_default_model = MagicMock(return_value="test-model")

        class _Gen:
            max_tokens = 8192

        self.generation = _Gen()

    async def _dispatch(self, **kwargs: Any) -> LLMResponse:
        kind = _kind(kwargs.get("messages", []))
        self.call_log.append(kind)
        if kind == "planner":
            return LLMResponse(content=_VALID_PLAN_JSON, tool_calls=[], usage={})
        if kind == "router":
            return LLMResponse(content=self.router_verdict, tool_calls=[], usage={})
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return LLMResponse(content="done", tool_calls=[], usage={})


def _spec(
    messages: list[dict[str, str]],
    *,
    planning_policy: PlanningPolicy | None = None,
    max_iterations: int = 30,
    **kwargs: Any,
) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=messages,
        tools=_make_tools(),
        model="test-model",
        max_iterations=max_iterations,
        max_tool_result_chars=1000,
        planning_policy=planning_policy,
        **kwargs,
    )


def _escalated(count: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        c
        for c in count
        if c.get("phase") == "plan_snapshot"
        and c.get("plan_snapshot", {}).get("origin") == "escalated"
    ]


def _stub_execute_tools(runner: AgentRunner) -> None:
    async def _fake_execute_tools(*args: Any, **kwargs: Any) -> tuple:
        return ([{"result": "ok"}], [], None)

    runner.tool_execution.execute_tools = _fake_execute_tools


async def _run(
    runner: AgentRunner,
    spec: AgentRunSpec,
) -> tuple[Any, list[dict[str, Any]]]:
    checkpoints: list[dict[str, Any]] = []

    async def _capture(payload: dict[str, Any]) -> None:
        checkpoints.append(payload)

    spec.checkpoint_callback = _capture
    result = await runner.run(spec)
    return result, checkpoints


def _direct_spec(**kwargs: Any) -> AgentRunSpec:
    return _spec(
        [{"role": "user", "content": _GRAY_TASK}],
        planning_policy=PlanningPolicy(),  # force_plan=None -> L1 -> GRAY -> L2 DIRECT
        **kwargs,
    )


# ---------------------------------------------------------------------------
# D1: Drift triggers after >=8 receipt-tool calls on an unplanned turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_escalation_triggers_after_eight_receipt_tools() -> None:
    """D1: GRAY task routed DIRECT (unplanned). After the 8th write_file, the
    turn escalates: a plan is created, origin='escalated', and later writes do
    not re-trigger (escalated_this_turn + counter reset).
    """
    responses = [_write(i) for i in range(8)] + [_write(i) for i in range(8, 12)] + [_text("done")]
    provider = _FakeProvider(responses)
    runner = AgentRunner(provider)
    _stub_execute_tools(runner)
    spec = _direct_spec()

    result, checkpoints = await _run(runner, spec)

    assert result.plan is not None
    assert result.plan.goal == "drifted goal"

    # Exactly one escalated snapshot (the escalation), and the post-escalation
    # writes did not re-trigger (escalated_this_turn + counters zeroed).
    assert len(_escalated(checkpoints)) == 1

    # router(DIRECT) + one escalation planner call; no further planner calls.
    assert provider.call_log.count("router") == 1
    assert provider.call_log.count("planner") == 1


# ---------------------------------------------------------------------------
# D2: Below threshold does not trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_below_threshold_no_escalation() -> None:
    """D2: Exactly 7 write_file calls then a plain-text close -> no escalation
    and no planner call at all.
    """
    responses = [_write(i) for i in range(7)] + [_text("done")]
    provider = _FakeProvider(responses)
    runner = AgentRunner(provider)
    _stub_execute_tools(runner)
    spec = _direct_spec()

    result, checkpoints = await _run(runner, spec)

    assert len(_escalated(checkpoints)) == 0
    assert result.plan is None
    assert provider.call_log.count("planner") == 0
    assert provider.call_log.count("router") == 1


# ---------------------------------------------------------------------------
# D3: Non-receipt tools do not count toward the drift threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_receipt_tools_not_counted() -> None:
    """D3: read_file/exec calls plus 7 write_file (10 tool calls total) -> no
    escalation, proving only RECEIPT_TOOLS count.
    """
    responses = (
        [_nonreceipt("read_file", 0), _nonreceipt("exec", 0), _nonreceipt("read_file", 1)]
        + [_write(i) for i in range(7)]
        + [_text("done")]
    )
    provider = _FakeProvider(responses)
    runner = AgentRunner(provider)
    _stub_execute_tools(runner)
    spec = _direct_spec()

    result, checkpoints = await _run(runner, spec)

    assert len(_escalated(checkpoints)) == 0
    assert result.plan is None
    assert provider.call_log.count("planner") == 0


# ---------------------------------------------------------------------------
# D4: Count accumulates across iterations (not reset per iteration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_count_accumulates_across_iterations() -> None:
    """D4: 2 write_file per iteration across 4 iterations = 8 -> escalation
    fires after the 4th iteration, proving the count is not per-iteration.
    """
    responses = (
        [_write(0), _write(1)]
        + [_write(2), _write(3)]
        + [_write(4), _write(5)]
        + [_write(6), _write(7)]
        + [_text("done")]
    )
    provider = _FakeProvider(responses)
    runner = AgentRunner(provider)
    _stub_execute_tools(runner)
    spec = _direct_spec()

    result, checkpoints = await _run(runner, spec)

    assert result.plan is not None
    assert len(_escalated(checkpoints)) == 1


# ---------------------------------------------------------------------------
# D5: Planned turns never escalate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planned_turn_does_not_escalate() -> None:
    """D5: force_plan=True + valid plan + 10 write_file calls -> no
    origin='escalated' snapshot (in-plan writes are not counted).
    """
    responses = [_write(i) for i in range(10)] + [_text("done")]
    provider = _FakeProvider(responses)
    runner = AgentRunner(provider)
    _stub_execute_tools(runner)
    spec = _spec(
        [{"role": "user", "content": _GRAY_TASK}],
        planning_policy=PlanningPolicy(force_plan=True),
    )

    result, checkpoints = await _run(runner, spec)

    assert result.plan is not None
    assert len(_escalated(checkpoints)) == 0
    # Only the initial force_plan=True planner call; no escalation planner call.
    assert provider.call_log.count("planner") == 1
    assert provider.call_log.count("router") == 0


# ---------------------------------------------------------------------------
# D6: No double escalation after drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_double_escalation_after_drift() -> None:
    """D6: After a drift escalation, continued write_file calls do not trigger
    a second escalation. Router(1) + escalation planner(1) = 2 route/planner
    calls; a second escalation would raise planner to 2.
    """
    responses = (
        [_write(i) for i in range(8)]
        + [_write(i) for i in range(8, 20)]
        + [_text("done")]
    )
    provider = _FakeProvider(responses)
    runner = AgentRunner(provider)
    _stub_execute_tools(runner)
    spec = _direct_spec()

    result, checkpoints = await _run(runner, spec)

    assert result.plan is not None
    assert len(_escalated(checkpoints)) == 1
    assert provider.call_log.count("router") == 1
    assert provider.call_log.count("planner") == 1


# ---------------------------------------------------------------------------
# D7: Stall path unchanged (W11-3 S1 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stall_path_still_escalates_on_two_nontool_iterations() -> None:
    """D7: Unplanned turn with 2 consecutive no-tool iterations still stalls
    to an escalated plan (trigger='stall' in the log); the OR-trigger leaves
    the stall semantics untouched.
    """
    provider = _FakeProvider(
        responses=[
            _text("still thinking"),
            _text("still thinking more"),
            _text("finally done"),
        ]
    )
    runner = AgentRunner(provider)
    # keep_looping: plain-text responses are continuations so the stall counter
    # can accumulate to 2 (mirrors W11-3 S1).
    spec = _direct_spec(goal_active_predicate=lambda: True)

    result, checkpoints = await _run(runner, spec)

    assert result.plan is not None
    assert result.plan.goal == "drifted goal"
    assert len(_escalated(checkpoints)) == 1
    assert provider.call_log.count("planner") == 1


# ---------------------------------------------------------------------------
# D8: Routing zero regression (W11-2 R6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_plan_path_unchanged() -> None:
    """D8: GRAY task + router PLAN still proceeds to the planner to create a
    plan (routing layer untouched by this batch), with no escalated snapshot.
    """
    provider = _FakeProvider(responses=[_text("done")], router_verdict="PLAN")
    runner = AgentRunner(provider)
    spec = _direct_spec()

    result, checkpoints = await _run(runner, spec)

    assert result.plan is not None
    assert len(_escalated(checkpoints)) == 0
    assert provider.call_log.count("router") == 1
    assert provider.call_log.count("planner") == 1
