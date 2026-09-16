"""W11-2: L2 gray-zone LLM router — parse_router_verdict + init_planner routing.

GRAY tasks get one cheap same-model adjudication call (router). The router
response decides PLAN (proceed to planner) or DIRECT (plain ReAct). Accounts
the call under CallPurpose.PLANNER (D1); call shape mirrors Planner.create_plan
(D2/A3); failures fail-open to DIRECT.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from erza.agent.execution.planning import (
    extract_session_context,
    parse_router_verdict,
)
from erza.agent.planning_policy import PlanningPolicy
from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.ledger import CallLedger, CallPurpose, bind_call_ledger
from erza.providers.base import GenerationSettings, LLMProvider, LLMResponse

_PLAN_JSON = json.dumps(
    {
        "goal": "ship",
        "steps": [
            {"id": 1, "action": "run tests"},
            {"id": 2, "action": "commit"},
            {"id": 3, "action": "tag"},
        ],
    }
)


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _spec(messages: list[dict[str, Any]], **kwargs: Any) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=messages,
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=1000,
        **kwargs,
    )


def _gray_task() -> str:
    return "写个注释说明这段代码"


class _ScriptedProvider(LLMProvider):
    """Scripted provider: returns preset responses in order; records ledger via chat_with_retry."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self._calls: list[dict[str, Any]] = []
        self.generation = GenerationSettings()

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, tools=None, model=None, **kwargs: Any) -> LLMResponse:
        self._calls.append({"messages": messages, "tools": tools, "model": model})
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="", tool_calls=[], usage={})

    @property
    def call_count(self) -> int:
        return len(self._calls)


# --- 3.1 parse_router_verdict (pure function) --------------------------------


def test_parse_router_verdict_plan_variants() -> None:
    """R1: 'PLAN' / 'plan' / 'Plan.' -> True (word-boundary)."""
    for text in ("PLAN", "plan", "Plan."):
        assert parse_router_verdict(text) is True, text


def test_parse_router_verdict_direct_variants() -> None:
    """R2: 'DIRECT' / 'direct' -> False."""
    for text in ("DIRECT", "direct"):
        assert parse_router_verdict(text) is False, text


def test_parse_router_verdict_negative_plan_variants() -> None:
    """R2b: negative patterns containing 'plan' -> False."""
    for text in (
        "DIRECT — no plan needed",
        "don't plan, just do it",
        "skip the plan",
    ):
        assert parse_router_verdict(text) is False, text


@pytest.mark.parametrize(
    "text",
    ["", "嗯好的", "Let me think...", "directly", "I am going to do nothing"],
)
def test_parse_router_verdict_fail_open(text: str) -> None:
    """R3+R5: empty / non-verdict / 'directly' (word boundary) -> False."""
    assert parse_router_verdict(text) is False


def test_parse_router_verdict_plan_mid_line() -> None:
    """R4: mid-line 'plan' counts (word boundary, not position)."""
    assert parse_router_verdict("I think we should plan this out") is True


# --- 3.2 init_planner routing integration ------------------------------------


@pytest.mark.asyncio
async def test_gray_router_plan_leads_to_planner() -> None:
    """R6: gray task, router PLAN, then planner produces a valid plan."""
    provider = _ScriptedProvider(
        [
            LLMResponse(content="PLAN", tool_calls=[], usage={}),
            LLMResponse(content=_PLAN_JSON, tool_calls=[], usage={}),
        ]
    )
    runner = AgentRunner(provider)
    spec = _spec([{"role": "user", "content": _gray_task()}])

    planner, plan, task_text, _tools_summary = await runner.init_planner(spec)

    assert planner is not None
    assert plan is not None, "router PLAN must proceed to the planner"
    assert plan.max_replans == 3
    assert task_text == _gray_task()


@pytest.mark.asyncio
async def test_gray_router_direct_skips_planner() -> None:
    """R7: router DIRECT -> all-None, only the router call fires."""
    provider = _ScriptedProvider([LLMResponse(content="DIRECT", tool_calls=[], usage={})])
    runner = AgentRunner(provider)
    spec = _spec([{"role": "user", "content": _gray_task()}])

    result = await runner.init_planner(spec)

    assert result == (None, None, None, None)
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_strong_signal_skips_router() -> None:
    """R8: numbered list (>=2 lines) routes PLAN without a router call."""
    provider = _ScriptedProvider([LLMResponse(content=_PLAN_JSON, tool_calls=[], usage={})])
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "请执行：\n1. 检查\n2. 测试\n3. 提交"}],
    )

    _planner, plan, _t, _s = await runner.init_planner(spec)

    assert plan is not None
    assert provider.call_count == 1  # only the planner call, no router


@pytest.mark.asyncio
async def test_direct_route_fires_no_calls() -> None:
    """R9: greeting/single-question route -> zero LLM calls."""
    provider = _ScriptedProvider([])
    runner = AgentRunner(provider)
    for task in ("你好", "这个配置项是干嘛的？"):
        spec = _spec([{"role": "user", "content": task}])
        result = await runner.init_planner(spec)
        assert result == (None, None, None, None)
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_force_plan_skips_router() -> None:
    """R10: force_plan=True -> no router call, straight to planner."""
    provider = _ScriptedProvider([LLMResponse(content=_PLAN_JSON, tool_calls=[], usage={})])
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "any task"}],
        planning_policy=PlanningPolicy(force_plan=True),
    )

    _planner, plan, _t, _s = await runner.init_planner(spec)

    assert plan is not None
    assert provider.call_count == 1  # only the planner call, no router


@pytest.mark.asyncio
async def test_router_exception_fails_open_to_direct() -> None:
    """R11: router raises -> fail-open DIRECT (all-None), no raise."""
    provider = _ScriptedProvider([
        LLMResponse(content="boom", tool_calls=[], usage={})
    ])
    original_chat = provider.chat

    async def _exploding(*args: Any, **kwargs: Any) -> LLMResponse:
        raise RuntimeError("router offline")

    provider.chat = _exploding
    try:
        runner = AgentRunner(provider)
        spec = _spec([{"role": "user", "content": _gray_task()}])
        result = await runner.init_planner(spec)
        assert result == (None, None, None, None)
    finally:
        provider.chat = original_chat


@pytest.mark.asyncio
async def test_router_finish_error_fails_open_to_direct() -> None:
    """R12: router finish_reason == 'error' -> fail-open DIRECT."""
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="error",
                tool_calls=[],
                usage={},
                finish_reason="error",
            )
        ]
    )
    runner = AgentRunner(provider)
    spec = _spec([{"role": "user", "content": _gray_task()}])

    result = await runner.init_planner(spec)

    assert result == (None, None, None, None)


# --- 3.3 session context (D3) ------------------------------------------------


@pytest.mark.asyncio
async def test_router_receives_session_context() -> None:
    """R13: router user message carries first-user-message + count."""
    provider = _ScriptedProvider([LLMResponse(content="PLAN", tool_calls=[], usage={})])
    runner = AgentRunner(provider)
    spec = _spec(
        [
            {"role": "user", "content": "重构认证模块，分三步：第一步改 schema，第二步改接口，第三步补测试"},
            {"role": "user", "content": "继续，把剩下两步做完"},
        ]
    )

    await runner.init_planner(spec)

    router_user = provider._calls[0]["messages"][1]["content"]
    assert "First user message: 重构认证模块" in router_user
    assert "User messages so far: 2" in router_user


def test_extract_session_context_no_user() -> None:
    """R14a: no user messages -> (None, 0)."""
    assert extract_session_context([]) == (None, 0)
    assert extract_session_context([{"role": "system", "content": "x"}]) == (None, 0)


def test_extract_session_context_truncates_first_user() -> None:
    """R14b: first user >200 chars truncated to 200; count preserved."""
    long = "长" * 300
    first, count = extract_session_context(
        [
            {"role": "user", "content": long},
            {"role": "user", "content": "second"},
        ]
    )
    assert len(first) == 200
    assert count == 2


# --- 3.4 accounting and budget (D1) -----------------------------------------


@pytest.mark.asyncio
async def test_gray_router_accounts_two_planner_calls() -> None:
    """R15: ledger records exactly 2 CallPurpose.PLANNER (router + planner)."""
    provider = _ScriptedProvider(
        [
            LLMResponse(content="PLAN", tool_calls=[], usage={"prompt_tokens": 5, "completion_tokens": 1}),
            LLMResponse(content=_PLAN_JSON, tool_calls=[], usage={"prompt_tokens": 10, "completion_tokens": 2}),
        ]
    )
    runner = AgentRunner(provider)
    spec = _spec([{"role": "user", "content": _gray_task()}])
    ledger = CallLedger()

    async with bind_call_ledger(ledger):
        _planner, plan, _t, _s = await runner.init_planner(spec)

    assert plan is not None
    purposes = [r.purpose for r in ledger.records]
    assert purposes.count(CallPurpose.PLANNER) == 2
    assert CallPurpose.UNCLASSIFIED not in purposes
