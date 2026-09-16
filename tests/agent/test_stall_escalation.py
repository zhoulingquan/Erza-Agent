"""W11-3: L3a stall escalation — restored FAST->MANAGED semantics for new routing.

Covers: upgrade trigger (S1), already-planned turn skips (S2), at-most-once
per turn (S3), force_plan=False opt-out (S4), planning_policy=None skips
(S5), tool resets counter (S6), force_plan=True bypasses routing (S7), and
escalation failure degrades gracefully (S8).
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
    {"goal": "escalated goal", "steps": [{"id": 1, "action": "escalated step"}]}
)


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


class _FakeProvider:
    """Provider with an explicit main-loop response queue.

    Planner calls auto-return valid plan JSON; router calls return PLAN;
    main-loop calls consume from ``responses`` (or return a default 'done').
    """

    def __init__(self, responses: list[LLMResponse] | None = None):
        self._responses = list(responses) if responses else []
        self._idx = 0
        self.call_log: list[str] = []
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
            return LLMResponse(content="PLAN", tool_calls=[], usage={})
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return LLMResponse(content="done", tool_calls=[], usage={})


def _spec(
    messages: list[dict[str, str]],
    *,
    planning_policy: PlanningPolicy | None = None,
    checkpoint_callback: Any | None = None,
    max_iterations: int = 10,
    keep_looping: bool = False,
    **kwargs: Any,
) -> AgentRunSpec:
    """keep_looping=True wires goal_active_predicate=True so the runner treats
    each plain-text response as a continuation point (multi-iteration driver
    for the stall detector)."""
    goal_active_predicate: Any = None
    if keep_looping:

        def goal_active_predicate() -> bool:
            return True
    return AgentRunSpec(
        initial_messages=messages,
        tools=_make_tools(),
        model="test-model",
        max_iterations=max_iterations,
        max_tool_result_chars=1000,
        planning_policy=planning_policy,
        checkpoint_callback=checkpoint_callback,
        goal_active_predicate=goal_active_predicate,
        **kwargs,
    )


def _escalated(count: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        c
        for c in count
        if c.get("phase") == "plan_snapshot"
        and c.get("plan_snapshot", {}).get("origin") == "escalated"
    ]


async def _run(
    runner: AgentRunner,
    spec: AgentRunSpec,
    *,
    fail_force_plan: bool = False,
) -> tuple[Any, list[dict[str, Any]]]:
    checkpoints: list[dict[str, Any]] = []

    async def _capture(payload: dict[str, Any]) -> None:
        checkpoints.append(payload)

    spec.checkpoint_callback = _capture
    result = await runner.run(spec)
    return result, checkpoints


# ---------------------------------------------------------------------------
# S1: Upgrade triggers after 2 consecutive no-tool iterations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stall_escalation_triggers_after_two_nontool_iterations() -> None:
    """S1: Unplanned turn, 2 no-tool responses -> escalation: plan set,
    checkpoint has origin='escalated'.
    """
    provider = _FakeProvider(
        responses=[
            LLMResponse(content="still thinking", tool_calls=[], usage={}),
            LLMResponse(content="still thinking more", tool_calls=[], usage={}),
            LLMResponse(content="finally done", tool_calls=[], usage={}),
        ]
    )
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(),  # force_plan=None -> L1 -> DIRECT
        max_iterations=5,
        keep_looping=True,
    )

    result, checkpoints = await _run(runner, spec)

    # Escalation created a plan
    assert result.plan is not None
    assert result.plan.goal == "escalated goal"

    # Checkpoint queue has exactly one escalated snapshot
    assert len(_escalated(checkpoints)) == 1


# ---------------------------------------------------------------------------
# S2: Already-planned turn does not escalate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_planned_turn_no_escalation() -> None:
    """S2: force_plan=True spec -> planner returns plan before loop; after N
    no-tool iterations, no second planner call and no escalated snapshot.
    """
    provider = _FakeProvider()
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(force_plan=True),
        max_iterations=5,
    )

    result, checkpoints = await _run(runner, spec)

    # Only 1 planner call is the initial init_planner, none from escalation
    assert provider.call_log.count("planner") == 1

    # No escalated snapshot
    assert len(_escalated(checkpoints)) == 0


# ---------------------------------------------------------------------------
# S3: At most once per turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_at_most_once_per_turn() -> None:
    """S3: After escalation, further stalls do not re-trigger.
    Planner calls total = 1 (the escalation only; init_planner for DIRECT is none).
    """
    provider = _FakeProvider()
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(),
        max_iterations=7,
        keep_looping=True,
    )

    result, checkpoints = await _run(runner, spec)

    # Exactly 1 planner call (the escalation)
    assert provider.call_log.count("planner") == 1

    # Exactly 1 escalated snapshot
    assert len(_escalated(checkpoints)) == 1


# ---------------------------------------------------------------------------
# S4: Explicit force_plan=False -> no escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_plan_false_no_escalation() -> None:
    """S4: PlanningPolicy(force_plan=False) -> DIRECT, 2 no-tool iterations
    -> no planner call at all (L1 also suppressed).
    """
    provider = _FakeProvider()
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(force_plan=False),
        max_iterations=5,
    )

    result, _ = await _run(runner, spec)

    # Zero planner calls (L1 forces DIRECT and escalation gate blocks)
    assert provider.call_log.count("planner") == 0
    assert result.plan is None


# ---------------------------------------------------------------------------
# S5: planning_policy=None -> no escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_planning_policy_no_escalation() -> None:
    """S5: spec without planning_policy. The runner's init_planner builds a
    default policy for routing, but the escalation condition gate reads
    spec.planning_policy directly, which stays None -> escalation blocked.
    """
    provider = _FakeProvider()
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=None,
        max_iterations=5,
    )

    result, checkpoints = await _run(runner, spec)

    assert provider.call_log.count("planner") == 0
    assert result.plan is None
    assert len(_escalated(checkpoints)) == 0


# ---------------------------------------------------------------------------
# S6: Tool execution resets counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_execution_resets_stall_counter() -> None:
    """S6: Response sequence = text, tool, text, text -> stall threshold of 2
    is only reached at iteration 4 (the tool resets), not iteration 2.
    """
    text_resp = LLMResponse(content="thinking...", tool_calls=[], usage={})
    tool_resp = LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="tc1", name="echo", arguments={"text": "ok"})],
        usage={},
    )

    provider = _FakeProvider(responses=[text_resp, tool_resp, text_resp, text_resp])
    runner = AgentRunner(provider)

    async def _fake_execute_tools(*args: Any, **kwargs: Any) -> tuple:
        return ([{"result": "ok"}], [], None)

    runner.tool_execution.execute_tools = _fake_execute_tools

    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(),
        max_iterations=8,
        keep_looping=True,
    )

    result, checkpoints = await _run(runner, spec)

    # Escalation triggered once (after the reset, at the 4th response)
    assert len(_escalated(checkpoints)) == 1
    assert result.plan is not None


# ---------------------------------------------------------------------------
# S7: force_plan=True bypasses routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_plan_true_bypasses_routing() -> None:
    """S7: GRAY task text + init_planner(force_plan=True) directly -> plan
    created, no L2 router call recorded.
    """
    provider = _FakeProvider()
    runner = AgentRunner(provider)

    # GRAY task text ("写个注释说明这段代码" -> verb_gate -> GRAY)
    spec = _spec(
        [{"role": "user", "content": "写个注释说明这段代码"}],
        planning_policy=PlanningPolicy(),
        max_iterations=3,
    )

    planner, plan, task_text, tools_summary = await runner.init_planner(spec, force_plan=True)

    assert plan is not None
    assert plan.goal == "escalated goal"

    # force_plan bypasses both L1 and L2 -> no router call
    assert provider.call_log.count("router") == 0
    # Exactly one planner call
    assert provider.call_log.count("planner") == 1


# ---------------------------------------------------------------------------
# S8: Escalation failure degrades gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_failure_degrades_gracefully() -> None:
    """S8: Provider returns non-JSON for the force planner call -> plan stays
    None, no escalated snapshot, loop continues without crashing.
    """
    planner_n = {"n": 0}

    async def _dispatch(**kwargs: Any) -> LLMResponse:
        kind = _kind(kwargs.get("messages", []))
        if kind == "planner":
            planner_n["n"] += 1
            # Return non-JSON -> Planner produces fallback -> init_planner drops it
            return LLMResponse(content="sorry I cannot plan", tool_calls=[], usage={})
        return LLMResponse(content="no progress...", tool_calls=[], usage={})

    provider = _FakeProvider()
    provider.chat_with_retry = AsyncMock(side_effect=_dispatch)
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(),
        max_iterations=5,
        keep_looping=True,
    )

    result, checkpoints = await _run(runner, spec)

    # The force planner call was attempted (and, failing, left the counter
    # armed so it re-attempts on each stalled iteration — HEAD fail-soft).
    assert planner_n["n"] >= 1

    # No escalated snapshot, plan stays None, no crash
    assert len(_escalated(checkpoints)) == 0
    assert result.plan is None


# ---------------------------------------------------------------------------
# S8b: Escalation failure does NOT retry (at-most-once enforced)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stall_escalation_failure_no_retry() -> None:
    """S8b: Provider returns non-JSON for the force planner call. After the
    failed attempt, the 'escalated_this_turn' flag prevents any further
    planner calls (planner call count stays exactly 1).
    """
    planner_n = {"n": 0}

    async def _dispatch(**kwargs: Any) -> LLMResponse:
        kind = _kind(kwargs.get("messages", []))
        if kind == "planner":
            planner_n["n"] += 1
            return LLMResponse(content="sorry I cannot plan", tool_calls=[], usage={})
        return LLMResponse(content="no progress...", tool_calls=[], usage={})

    provider = _FakeProvider()
    provider.chat_with_retry = AsyncMock(side_effect=_dispatch)
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(),
        max_iterations=5,
        keep_looping=True,
    )

    result, checkpoints = await _run(runner, spec)

    # Exactly 1 planner call: the failed attempt counts, but no retry follows.
    assert planner_n["n"] == 1
    assert len(_escalated(checkpoints)) == 0
    assert result.plan is None


# ---------------------------------------------------------------------------
# S9: A real injection arrival resets the stall counter (E2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_resets_stall_counter() -> None:
    """S9: A real injection arrival (new user message) zeroes the stall
    counter, so the model answering fresh input is not treated as stalling.
    Escalation only fires after injections are exhausted and accumulation
    resumes (goal_continue does not reset the counter).
    """
    drain_info = {"n": 0, "at_escalation": None}

    async def _injection_callback() -> list[dict[str, Any]]:
        drain_info["n"] += 1
        if drain_info["n"] <= 3:
            return [{"role": "user", "content": f"follow-up {drain_info['n']}"}]
        return []

    provider = _FakeProvider(
        responses=[
            LLMResponse(content="still working", tool_calls=[], usage={}),
            LLMResponse(content="still working", tool_calls=[], usage={}),
            LLMResponse(content="still working", tool_calls=[], usage={}),
            LLMResponse(content="still working", tool_calls=[], usage={}),
            LLMResponse(content="still working", tool_calls=[], usage={}),
        ]
    )
    runner = AgentRunner(provider)

    spec = _spec(
        [{"role": "user", "content": "test task"}],
        planning_policy=PlanningPolicy(),
        max_iterations=10,
        keep_looping=True,
        injection_callback=_injection_callback,
    )

    async def _dispatch(**kwargs: Any) -> LLMResponse:
        if _kind(kwargs.get("messages", [])) == "planner":
            drain_info["at_escalation"] = drain_info["n"]
        return await provider._dispatch(**kwargs)

    provider.chat_with_retry = AsyncMock(side_effect=_dispatch)

    result, checkpoints = await _run(runner, spec)

    # Escalation fired only after all 3 real injections were drained — the
    # injection reset prevented an early trigger during the injection flow.
    assert drain_info["at_escalation"] is not None
    assert drain_info["at_escalation"] >= 3

    # And it did eventually escalate once accumulation resumed.
    assert provider.call_log.count("planner") == 1
    assert result.plan is not None
    assert result.plan.goal == "escalated goal"
    assert len(_escalated(checkpoints)) == 1
