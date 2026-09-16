"""W11-1: PlanningPolicy — L1 heuristic gate with three-value routing.

P4 baseline: deterministic ``should_plan(task_text)`` decides per turn whether
the task warrants a plan; ``force_plan`` provides a deterministic override.

W11-1: L1 upgraded from a bool to a three-value ``RouteDecision``
(``Route.PLAN / DIRECT / GRAY``). ``should_plan`` remains a compat facade that
is True only when L1 routes PLAN; GRAY is left for L2 adjudication (W11-2) and
is, in this batch, equivalent to DIRECT from the caller's perspective.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.loop import AgentLoop
from erza.agent.planning_policy import PlanningPolicy, Route
from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.bus.queue import MessageBus
from erza.providers.base import LLMProvider, LLMResponse


class FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _spec(messages: list[dict[str, str]], **kwargs: Any) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=messages,
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=1000,
        **kwargs,
    )


_VALID_CAUSES = {
    "forced",
    "empty",
    "strong_signal",
    "macro_goal",
    "verb_gate",
    "trivial",
    "no_signal",
}


# --- 4.1 Compat (should_plan facade) / classify core ------------------------


def test_force_plan_overrides_classify() -> None:
    """T1: force_plan=True/False overrides every rule in classify."""
    assert PlanningPolicy(force_plan=True).classify("hi").route is Route.PLAN
    assert PlanningPolicy(force_plan=True).classify("").route is Route.PLAN
    assert PlanningPolicy(force_plan=False).classify("hi").route is Route.DIRECT
    plan_task = "请执行以下步骤：\n1. 检查\n2. 测试"
    assert PlanningPolicy(force_plan=False).classify(plan_task).route is Route.DIRECT
    forced = PlanningPolicy(force_plan=True).classify("hi")
    assert forced.cause == "forced"


def test_classify_empty_or_blank_is_direct() -> None:
    """T2: empty / None / whitespace -> DIRECT."""
    for task in (None, "", "   ", "\n\t "):
        decision = PlanningPolicy().classify(task)
        assert decision.route is Route.DIRECT
        assert decision.cause == "empty"


def test_numbered_list_task_plans() -> None:
    """T3: numbered list with >=2 lines -> PLAN (strong_signal), zh and en."""
    zh = PlanningPolicy().classify("请执行以下步骤：\n1. 检查配置\n2. 运行测试\n3. 提交代码")
    assert zh.route is Route.PLAN
    assert zh.cause == "strong_signal"
    en = PlanningPolicy().classify("Do these steps:\n1. read\n2. write\n3. commit")
    assert en.route is Route.PLAN
    assert en.cause == "strong_signal"


def test_inline_numbered_sequence_plans() -> None:
    """T4: inline numbered sequence -> PLAN."""
    decision = PlanningPolicy().classify("1. read 2. write 3. commit")
    assert decision.route is Route.PLAN
    assert decision.cause == "strong_signal"


def test_long_task_plans_by_effective_length() -> None:
    """T5: >=300 effective-length triggers PLAN; zh 300 and en ~800 chars."""
    zh = PlanningPolicy().classify("详细说明内容" * 60)  # 360 CJK -> eff 360
    assert zh.route is Route.PLAN
    assert zh.cause == "strong_signal"
    en = PlanningPolicy().classify("sample prose " * 60)  # 780 chars -> eff ~312
    assert en.route is Route.PLAN
    assert en.cause == "strong_signal"


def test_plain_english_paragraph_is_not_planned() -> None:
    """T6: ~60-word (~350 char, eff ~140) plain paragraph -> not PLAN (GRAY)."""
    paragraph = (
        "The weather is very nice today and the garden looks lovely. "
        "We walked along the quiet path and watched the birds fly over the "
        "field. The sun felt warm and the air was fresh and clean. Everyone "
        "seemed happy to be outside for a little while this afternoon."
    )
    decision = PlanningPolicy().classify(paragraph)
    assert decision.route is Route.GRAY
    assert PlanningPolicy().should_plan(paragraph) is False


# --- 4.2 Defect regressions (F1-F4) -----------------------------------------


def test_single_bulleted_line_is_not_planned() -> None:
    """T7 (E1.1 flip): a single line like '- 好的没问题' -> DIRECT (trivial, eff_len < 24, no productive verb)."""
    decision = PlanningPolicy().classify("- 好的没问题")
    assert decision.route is Route.DIRECT  # E1.1 flipset: T7
    assert decision.cause == "trivial"  # E1.1 flipset: T7
    assert PlanningPolicy().should_plan("- 好的没问题") is False


def test_git_diff_fragment_is_not_planned() -> None:
    """T8 (F1): a pasted git diff (one removal line, short) does not plan."""
    diff = (
        "diff --git a/readme.md b/readme.md\n"
        "index 1a2b3c..4d5e6f 100644\n"
        "--- a/readme.md\n"
        "+++ b/readme.md\n"
        "@@ -1 +1 @@\n"
        "-remove the old phrase here\n"
        "+add the new phrase here\n"
    )
    decision = PlanningPolicy().classify(diff)
    assert decision.route is Route.GRAY
    assert PlanningPolicy().should_plan(diff) is False


def test_bare_za_no_longer_counts_as_step_marker() -> None:
    """T9 (E1.1 flip): '先看下这个，然后再告诉我' -> DIRECT (trivial, eff_len < 24, no productive verb)."""
    text = "先看下这个，然后再告诉我"
    decision = PlanningPolicy().classify(text)
    assert decision.route is Route.DIRECT  # E1.1 flipset: T9
    assert decision.cause == "trivial"  # E1.1 flipset: T9
    assert PlanningPolicy().should_plan(text) is False


def test_two_distinct_chinese_markers_plan() -> None:
    """T10: '然后' + '最后' -> 3 zh markers, PLAN."""
    decision = PlanningPolicy().classify("查一下A，然后对比B，最后给结论")
    assert decision.route is Route.PLAN
    assert decision.cause == "strong_signal"
    assert PlanningPolicy().should_plan("查一下A，然后对比B，最后给结论") is True


def test_sentence_initial_english_markers_plan() -> None:
    """T11 (F3): First/Then/Finally matched via casefold+word-boundary -> PLAN."""
    text = "First, read the file. Then fix it. Finally, run tests."
    decision = PlanningPolicy().classify(text)
    assert decision.route is Route.PLAN
    assert decision.cause == "strong_signal"


def test_single_english_marker_is_not_planned() -> None:
    """T12 (E1.1 flip): 'Then we left.' -> DIRECT (trivial, eff_len < 24, no productive verb)."""
    decision = PlanningPolicy().classify("Then we left.")
    assert decision.route is Route.DIRECT  # E1.1 flipset: T12
    assert decision.cause == "trivial"  # E1.1 flipset: T12
    assert PlanningPolicy().should_plan("Then we left.") is False


# --- 4.3 Macro-goal gate (F6) -----------------------------------------------


def test_macro_goal_zh_plans() -> None:
    """T13 (F6): '做一个射击游戏' -> PLAN macro_goal (verb=做, product=游戏)."""
    decision = PlanningPolicy().classify("给我做一个射击游戏")
    assert decision.route is Route.PLAN
    assert decision.cause == "macro_goal"
    assert decision.signals == {"verb": "做", "product": "游戏"}


def test_macro_goal_en_plans() -> None:
    """T14 (F6): 'build me a snake game' -> PLAN (build + game)."""
    decision = PlanningPolicy().classify("build me a snake game")
    assert decision.route is Route.PLAN
    assert decision.cause == "macro_goal"
    assert decision.signals == {"verb": "build", "product": "game"}


def test_macro_goal_shorts_task_plans() -> None:
    """T15 (F6): '做个小工具' -> PLAN (做 x 工具)."""
    decision = PlanningPolicy().classify("做个小工具")
    assert decision.route is Route.PLAN
    assert decision.cause == "macro_goal"
    assert decision.signals == {"verb": "做", "product": "工具"}


# --- 4.4 Gray zone and trivial ----------------------------------------------


def test_verb_without_product_is_gray() -> None:
    """T16: '写个注释...' -> GRAY (verb_gate)."""
    decision = PlanningPolicy().classify("写个注释说明这段代码")
    assert decision.route is Route.GRAY
    assert decision.cause == "verb_gate"
    assert decision.signals == {"verb": "写"}


def test_greetings_are_direct() -> None:
    """T17: '你好' / 'thanks' / '好的，收到' -> DIRECT (trivial)."""
    for task in ("你好", "thanks", "好的，收到"):
        decision = PlanningPolicy().classify(task)
        assert decision.route is Route.DIRECT, task
        assert decision.cause == "trivial", task


def test_single_questions_are_direct() -> None:
    """T18: single questions -> DIRECT (trivial)."""
    for task in ("这个配置项是干嘛的？", "what does this flag do?"):
        decision = PlanningPolicy().classify(task)
        assert decision.route is Route.DIRECT, task
        assert decision.cause == "trivial", task


def test_greeting_with_following_task_is_trivial_direct() -> None:
    """T19 (E1.1): '你好，帮我看看今天日程' -> DIRECT (short imperative, no productive verb)."""
    decision = PlanningPolicy().classify("你好，帮我看看今天日程")
    assert decision.route is Route.DIRECT
    assert decision.cause == "trivial"


def test_no_signal_ordering_task_is_gray() -> None:
    """T20: an analyse/compare task with no strong signals -> GRAY."""
    text = "查一下这个订单为什么没发货，对比库存和物流记录再下结论"
    decision = PlanningPolicy().classify(text)
    assert decision.route is Route.GRAY
    assert decision.cause == "no_signal"


def test_non_productive_verb_is_trivial_direct() -> None:
    """T21 (E1.1): '跑一下测试' -> DIRECT (short, no productive verb, eff_len < 24)."""
    decision = PlanningPolicy().classify("跑一下测试")
    assert decision.route is Route.DIRECT
    assert decision.cause == "trivial"


def test_e1_short_imperative_chinese_is_direct() -> None:
    """E1 regression: '看看这个文件' -> DIRECT (no productive verb, eff_len < 24)."""
    decision = PlanningPolicy().classify("看看这个文件")
    assert decision.route is Route.DIRECT
    assert decision.cause == "trivial"
    assert "effective_len" in decision.signals


def test_e1_short_imperative_english_is_direct() -> None:
    """E1 regression: 'ship' -> DIRECT (no productive verb, eff_len < 24)."""
    decision = PlanningPolicy().classify("ship")
    assert decision.route is Route.DIRECT
    assert decision.cause == "trivial"


def test_e1_verb_short_task_stays_gray() -> None:
    """E1 regression: '写个注释' -> GRAY (productive verb present despite short length)."""
    decision = PlanningPolicy().classify("写个注释")
    assert decision.route is Route.GRAY
    assert decision.cause == "verb_gate"


# --- 4.5 signals audit ------------------------------------------------------


def test_signals_carry_measured_evidence() -> None:
    """T22: signals hold the firing evidence (list_lines / markers / verb)."""
    list_task = PlanningPolicy().classify("1. 读取\n2. 分析\n3. 写入")
    assert list_task.signals["list_lines"] == 3
    marker_task = PlanningPolicy().classify("查一下A，然后对比B，最后给结论")
    assert marker_task.signals["markers"] == ["然后", "最后"]
    macro_task = PlanningPolicy().classify("给我做一个射击游戏")
    assert macro_task.signals == {"verb": "做", "product": "游戏"}


def test_all_causes_are_members_of_the_enum() -> None:
    """T23: every cause value belongs to the documented set."""
    tasks = [
        PlanningPolicy(force_plan=True).classify("hi"),
        PlanningPolicy(force_plan=False).classify("hi"),
        PlanningPolicy().classify(None),
        PlanningPolicy().classify("1. a\n2. b"),
        PlanningPolicy().classify("给我做一个射击游戏"),
        PlanningPolicy().classify("写个注释说明这段代码"),
        PlanningPolicy().classify("你好"),
        PlanningPolicy().classify("这个配置项是干嘛的？"),
        PlanningPolicy().classify("跑一下测试"),
    ]
    assert {d.cause for d in tasks} <= _VALID_CAUSES


# --- should_plan compat facade ----------------------------------------------


def test_should_plan_empty_or_none_task_is_false() -> None:
    assert PlanningPolicy().should_plan(None) is False
    assert PlanningPolicy().should_plan("") is False


def test_should_plan_trivial_task_is_false() -> None:
    assert PlanningPolicy().should_plan("hello there") is False


def test_should_plan_numbered_list_task_is_true() -> None:
    policy = PlanningPolicy()
    assert policy.should_plan("请执行以下步骤：\n1. 检查配置\n2. 运行测试\n3. 提交代码") is True
    assert policy.should_plan("Do these steps: 1. read 2. write 3. commit") is True


def test_should_plan_bulleted_list_task_is_true() -> None:
    policy = PlanningPolicy()
    assert policy.should_plan("请帮我：\n- 读取文件\n- 修改代码\n- 推送") is True


def test_should_plan_two_distinct_step_markers_is_true() -> None:
    policy = PlanningPolicy()
    assert policy.should_plan("首先分析需求，然后编写代码，最后运行测试") is True
    assert policy.should_plan("First read the config, then patch, after that run the tests") is True


def test_should_plan_long_task_is_true() -> None:
    policy = PlanningPolicy()
    long_task = "任务：" + "详细说明内容 " * 50  # > 400 chars
    assert policy.should_plan(long_task) is True


def test_should_plan_long_but_plain_text_is_false() -> None:
    """A long sentence without step markers or lists stays plain ReAct."""
    policy = PlanningPolicy()
    text = "请给我写一篇关于夏天度假的文章，要包含海滩、美食、风景和历史文化。"
    assert policy.should_plan(text) is False


def test_force_plan_overrides_heuristics() -> None:
    assert PlanningPolicy(force_plan=True).should_plan("hi") is True
    assert PlanningPolicy(force_plan=False).should_plan("请执行以下步骤：\n1. 检查\n2. 测试") is False


# --- AgentLoop resolution ---------------------------------------------------


def test_agent_loop_accepts_direct_planning_policy(tmp_path) -> None:
    policy = PlanningPolicy(force_plan=True)
    loop = AgentLoop(
        bus=MessageBus(),
        workspace=tmp_path,
        provider=FakeProvider(),
        planning_policy=policy,
    )
    assert loop.planning_policy is policy


def test_agent_loop_builds_default_routing_policy(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        workspace=tmp_path,
        provider=FakeProvider(),
        planner_max_replans=7,
    )
    assert loop.planning_policy is not None
    assert loop.planning_policy.planner_max_replans == 7
    assert loop.planning_policy.force_plan is None


# --- AgentRunSpec propagation ------------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_spec_carries_planning_policy(tmp_path) -> None:
    from erza.bus.events import InboundMessage
    from erza.config.schema import Config

    config = Config.model_validate(
        {
            "agents": {"defaults": {"plannerMaxReplans": 2}},
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )
    loop = AgentLoop.from_config(config, provider=FakeProvider())
    captured_spec: AgentRunSpec | None = None
    original_run = loop.runner.run

    async def capturing_run(spec: Any) -> Any:
        nonlocal captured_spec
        captured_spec = spec
        return await original_run(spec)

    loop.runner.run = capturing_run

    await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="test",
            content="hello",
        )
    )

    assert captured_spec is not None
    assert captured_spec.planning_policy is not None
    assert captured_spec.planning_policy.planner_max_replans == 2


# --- init_planner routes by task ---------------------------------------------


@pytest.mark.asyncio
async def test_trivial_task_skips_planner() -> None:
    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "ship"}],
    )

    result = await runner.init_planner(spec)

    assert result == (None, None, None, None)
    assert not provider.chat_with_retry.called


@pytest.mark.asyncio
async def test_multi_step_task_invokes_planner_with_execution_model() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "test"}]}),
            tool_calls=[],
            usage={},
        )
    )
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "首先分析，然后实现，最后验证"}],
    )

    planner, plan, task_text, _tools_summary = await runner.init_planner(spec)

    assert planner is not None
    assert plan is not None
    assert plan.goal == "ship"
    assert task_text == "首先分析，然后实现，最后验证"
    # Single-model: planner reused the execution model.
    assert provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_force_plan_invokes_planner_for_trivial_task() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps({"goal": "x", "steps": [{"id": 1, "action": "a"}]}),
            tool_calls=[],
            usage={},
        )
    )
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "ship"}],
        planning_policy=PlanningPolicy(force_plan=True),
    )

    _planner, plan, _task, _summary = await runner.init_planner(spec)

    assert plan is not None
    assert plan.goal == "x"
