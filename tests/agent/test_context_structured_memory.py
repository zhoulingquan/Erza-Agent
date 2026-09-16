"""ContextBuilder single-path structured memory injection tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from erza.agent.context import ContextBuilder
from erza.config.schema import StructuredMemoryConfig
from erza.memory import MemoryStore
from erza.memory.lifecycle import IngestContext
from erza.memory.models import (
    ActorKind,
    EvidenceKind,
    EvidenceRef,
    MemoryScope,
    RecallResult,
    ScopeKind,
)

UTC = timezone.utc

RECALL_HEADER = "# Recalled Memory (Deterministic)"


def make_proposal(statement: str, slot: str = "memory.retrieval.strategy", **overrides):
    p = {
        "proposal_index": 0,
        "kind": "decision",
        "scope_hint": "project",
        "subject": "Erza",
        "slot": slot,
        "statement": statement,
        "detail": "",
        "tags": ["architecture.memory"],
        "aliases": [],
        "confidence": 1.0,
        "importance": 5,
        "evidence_refs": ["history:1"],
        "speech_act": "confirmed_decision",
        "expires_at": None,
    }
    p.update(overrides)
    return p


def seed_active_record(
    store: MemoryStore,
    statement: str,
    slot: str = "memory.retrieval.strategy",
    scope: MemoryScope | None = None,
    importance: int = 5,
):
    """Ingest a proposal and promote it to ACTIVE via the lifecycle.

    Uses a MANUAL (hard) evidence ref so the promoted record carries a
    CONFIRMED_DECISION source level, which is what the resident profile block
    (plan B2) filters on.
    """
    evidence_catalog = {
        "command:msg-42": EvidenceRef(
            kind=EvidenceKind.MANUAL,
            ref="command:msg-42",
            excerpt=statement,
            observed_at=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
        )
    }
    from erza.memory.extraction import parse_extraction_batch

    extracted = parse_extraction_batch(
        json.dumps(
            {
                "schema_version": 1,
                "proposals": [
                    make_proposal(
                        statement,
                        slot,
                        importance=importance,
                        evidence_refs=("command:msg-42",),
                    )
                ],
            }
        ),
        evidence_catalog,
        store.structured_repository.tag_catalog,
    )
    context = IngestContext(
        actor=ActorKind.DREAM,
        reason="test seed",
        source_batch=f"seed:{statement}",
        scope=scope or MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
        evidence_catalog=evidence_catalog,
        now=datetime.now(UTC),
    )
    result = store.structured_lifecycle.ingest(extracted.proposals[0], context)
    if result.final_status.value != "active":
        store.structured_lifecycle.promote(
            result.candidate_id,
            actor=ActorKind.SYSTEM,
            reason="test seed promote",
        )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agent\n- Workflow rule\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Soul\n- Helpful\n", encoding="utf-8")
    # Removed files may still exist in a developer directory, but runtime
    # ignores them and never mutates them.
    (tmp_path / "USER.md").write_text("# User\n- Alice the developer\n", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("# Memory\n- Removed memory fact\n", encoding="utf-8")
    return tmp_path


def make_builder(workspace, **kwargs) -> ContextBuilder:
    config = StructuredMemoryConfig(**kwargs) if kwargs else None
    return ContextBuilder(workspace, structured_memory_config=config)


class TestStructuredMemoryContext:
    def test_omits_legacy_memory_and_shared(self, workspace):
        shared = workspace / "memory" / "shared" / "MEMORY_SHARED.md"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("- shared legacy fact\n", encoding="utf-8")
        builder = make_builder(workspace)

        prompt = builder.build_system_prompt()

        assert "Removed memory fact" not in prompt
        assert "shared legacy fact" not in prompt

    def test_omits_user_bootstrap_keeps_agent_and_soul(self, workspace):
        builder = make_builder(workspace)

        prompt = builder.build_system_prompt()

        assert "Alice the developer" not in prompt
        assert "Workflow rule" in prompt
        assert "Helpful" in prompt

    def test_skips_recall_without_query(self, workspace):
        builder = make_builder(workspace)
        seed_active_record(builder.memory, "Main uses deterministic structured recall.")

        # Empty current message (e.g. subagent resume turns) yields no recall.
        messages = builder.build_messages(history=[], current_message="")

        user = str(messages[-1]["content"])
        assert RECALL_HEADER not in user

    def test_injects_recall_hits_into_user_tail(self, workspace):
        builder = make_builder(workspace)
        seed_active_record(builder.memory, "Main uses deterministic structured recall.")

        messages = builder.build_messages(
            history=[], current_message="architecture.memory recall strategy"
        )

        user = str(messages[-1]["content"])
        system = messages[0]["content"]
        assert RECALL_HEADER in user
        assert "deterministic structured recall" in user
        # W10-C2: recall must not leak into the frozen system prefix.
        assert RECALL_HEADER not in system

    def test_injects_custom_policy(self, workspace):
        policy = workspace / "memory" / "shared" / "POLICY.md"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text("Never modify production configs without approval.\n", encoding="utf-8")
        builder = make_builder(workspace)

        prompt = builder.build_system_prompt()

        assert "Never modify production configs without approval" in prompt

    def test_build_messages_feeds_current_message_as_recall_query(self, workspace):
        builder = make_builder(workspace)
        seed_active_record(builder.memory, "Erza recall stays local without embeddings.")

        messages = builder.build_messages(
            history=[], current_message="how does Erza recall memory?"
        )

        user = str(messages[-1]["content"])
        assert RECALL_HEADER in user
        assert "stays local" in user

    def test_governed_recall_degraded_injects_diagnostic_without_facts(
        self, workspace, monkeypatch
    ):
        builder = make_builder(workspace)
        monkeypatch.setattr(
            builder.memory,
            "recall_structured",
            lambda _query: RecallResult(
                degraded=True,
                error_code="journal_corrupt",
                error_message="invalid transaction",
            ),
        )

        messages = builder.build_messages(history=[], current_message="Erza memory")
        user = str(messages[-1]["content"])

        assert "Structured memory recall is unavailable" in user
        assert "journal_corrupt" in user
        assert "invalid transaction" not in user
        assert RECALL_HEADER not in user

    def test_build_messages_recall_includes_exact_session_and_user_scopes(self, workspace):
        builder = make_builder(workspace)
        seed_active_record(
            builder.memory,
            "Alice prefers compact responses.",
            slot="response.style",
            scope=MemoryScope(kind=ScopeKind.USER, key="user:alice"),
        )
        seed_active_record(
            builder.memory,
            "This session is debugging caching.",
            slot="session.topic",
            scope=MemoryScope(kind=ScopeKind.SESSION, key="session:web:chat-7"),
        )

        messages = builder.build_messages(
            history=[],
            current_message="Erza Alice caching response session",
            sender_id="alice",
            session_key="web:chat-7",
        )

        user = str(messages[-1]["content"])
        assert "Alice prefers compact responses" in user
        assert "This session is debugging caching" in user

    def test_build_messages_uses_default_user_scope_without_sender(self, workspace):
        builder = make_builder(workspace)
        seed_active_record(
            builder.memory,
            "Default user prefers Chinese.",
            slot="response.language",
            scope=MemoryScope(kind=ScopeKind.USER, key="user:default"),
        )

        messages = builder.build_messages(
            history=[],
            current_message="Erza default user language",
            session_key="cli:direct",
        )

        assert "Default user prefers Chinese" in str(messages[-1]["content"])

    def test_subagent_scope_uses_parent_session_and_user_identity(self, workspace):
        builder = make_builder(workspace)
        seed_active_record(
            builder.memory,
            "Parent user wants terse output.",
            slot="response.style",
            scope=MemoryScope(kind=ScopeKind.USER, key="user:alice"),
        )

        messages = builder.build_messages(
            history=[{"role": "user", "content": "task", "sender_id": "alice"}],
            current_message="Erza parent user terse",
            sender_id="subagent",
            session_key="web:chat-7#sub:task-1",
            memory_user_key="user:alice",
        )

        assert "Parent user wants terse output" in str(messages[-1]["content"])

def test_resident_profile_block(workspace) -> None:
    """Always-on user profile block: rendering, filtering, skip, budget.

    Plan B2 acceptance — the resident block is an enhancement, never a
    critical path: disabled or degraded stores are silently skipped, and the
    whole block lives in the dynamic user-message tail (W10-C2), never in the
    frozen ``build_system_prompt`` prefix.
    """
    # -- 高置信 USER 记录渲染进常驻画像块 -------------------------------
    builder = make_builder(workspace)
    seed_active_record(
        builder.memory,
        "回复始终使用简体中文",
        slot="response.language",
        scope=MemoryScope(kind=ScopeKind.USER, key="user:default"),
        importance=5,
    )
    seed_active_record(
        builder.memory,
        "回复使用简洁句式",
        slot="response.style",
        scope=MemoryScope(kind=ScopeKind.USER, key="user:default"),
        importance=2,
    )

    section = builder._resident_profile_section(user_key="user:default")
    assert section.startswith("# User Profile (Always-On)")
    assert "回复始终使用简体中文" in section
    # importance=2 的 USER 记录不出现
    assert "回复使用简洁句式" not in section
    # 每条一行、带稳定 mem id、不带 Why 评分理由（保持紧凑）
    assert "- [mem_" in section
    assert "Why:" not in section

    # -- 注入点：动态块进 user 消息尾部，不进 build_system_prompt ---------
    messages = builder.build_messages(
        history=[],
        current_message="查一下我的偏好",
        session_key="cli:direct",
    )
    user = str(messages[-1]["content"])
    system = messages[0]["content"]
    assert "# User Profile (Always-On)" in user
    assert "# User Profile (Always-On)" not in system
    # 常驻块（偏好类记忆）优先于 recall 段
    if RECALL_HEADER in user:
        assert user.find("# User Profile (Always-On)") < user.find(RECALL_HEADER)

    # -- resident_profile_enabled=False 返回 "" ---------------------------
    disabled = ContextBuilder(
        workspace,
        structured_memory_config=StructuredMemoryConfig(resident_profile_enabled=False),
    )
    assert disabled._resident_profile_section(user_key="user:default") == ""

    # -- token 预算截断：条目数减少而非截断半行 ---------------------------
    long_five = "遵循确定性规则重复执行稳定流程以降低出错概率" * 13  # ~286 chars
    long_four = "提交前运行全量回归验证改动未破坏既有行为" * 13  # ~286 chars
    tight = ContextBuilder(
        workspace,
        structured_memory_config=StructuredMemoryConfig(resident_profile_token_budget=100),
    )
    seed_active_record(
        tight.memory,
        long_five,
        slot="procedure.step",
        scope=MemoryScope(kind=ScopeKind.USER, key="user:budget"),
        importance=5,
    )
    seed_active_record(
        tight.memory,
        long_four,
        slot="procedure.check",
        scope=MemoryScope(kind=ScopeKind.USER, key="user:budget"),
        importance=4,
    )
    budget_section = tight._resident_profile_section(user_key="user:budget")
    assert budget_section.count("- [") == 1
    # 完整行保留，无半行截断
    assert long_five in budget_section
    assert long_four not in budget_section

def test_policy_over_budget_injects_action_required(workspace) -> None:
    """Plan C2: an oversized shared policy must be injected in full with an
    ACTION REQUIRED note — never silently truncated, and the maintenance path
    must point at /memory-* commands instead of a direct file edit."""
    policy_path = workspace / "memory" / "shared" / "POLICY.md"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    long_policy = "Never modify production configs without approval.\n" + "f" * 300_000
    policy_path.write_text(long_policy, encoding="utf-8")

    prompt = make_builder(workspace).build_system_prompt()

    assert "ACTION REQUIRED" in prompt
    assert "POLICY.md" in prompt
    assert "/memory-correct" in prompt
    # policy 正文未被截断：完整保留在产物中
    assert long_policy in prompt


def test_policy_within_budget_has_no_action_required(workspace) -> None:
    policy_path = workspace / "memory" / "shared" / "POLICY.md"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text("# Policy\n- Be concise.\n", encoding="utf-8")

    prompt = make_builder(workspace).build_system_prompt()

    assert "ACTION REQUIRED" not in prompt
    assert "Be concise." in prompt
