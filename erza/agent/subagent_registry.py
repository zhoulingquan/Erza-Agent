"""Declarative subagent registry (TRAE-style .md definitions).

Subagents are defined as Markdown files with YAML frontmatter in the
workspace's `agents/` directory. The main agent's system prompt lists
their `description` so the LLM can autonomously delegate via the
`delegate` tool (mirrors TRAE's built-in Agent → Subagent dispatch).

The implementation lives in :mod:`erza.contracts.subagent` (the shared
agent/tools contract package). This module re-exports it so existing
``from erza.agent.subagent_registry import ...`` call sites keep working;
import from ``erza.contracts.subagent`` in new code.
"""

from __future__ import annotations

from erza.contracts.subagent import SubagentDefinition, SubagentRegistry

__all__ = ["SubagentDefinition", "SubagentRegistry"]
