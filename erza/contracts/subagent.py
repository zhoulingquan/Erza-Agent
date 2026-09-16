"""Subagent contract types shared by the agent core and the tools layer.

``SubagentDefinition`` / ``SubagentRegistry`` moved verbatim from
``erza.agent.subagent_registry``; ``SubagentStatus`` and the recursion-depth
contextvar moved from ``erza.agent.subagent``. This keeps the delegate /
spawn / self tools' contract surface free of any ``erza.agent`` import.

``erza.agent.subagent_registry`` and ``erza.agent.subagent`` re-export the
symbols below; prefer importing from ``erza.contracts.subagent`` in new
code.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from loguru import logger

# 子代理递归深度跟踪：主代理 depth=0，子代理 depth=1，孙代理 depth=2。
# delegate tool 在运行时读取此 ContextVar 决定是否允许再 delegate。
# 默认值 0 表示主代理上下文（未进入子代理 run 时）。
_current_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_subagent_current_depth", default=0
)


def get_current_subagent_depth() -> int:
    """返回当前上下文的子代理递归深度。

    主代理运行时返回 0；子代理运行时返回 1；孙代理返回 2，依此类推。
    delegate tool 用此值与 ``max_subagent_recursion_depth`` 比较来决定是否允许再 delegate。
    """
    return _current_depth.get()


def bind_subagent_depth(depth: int) -> contextvars.Token[int]:
    """Set the subagent recursion depth; returns a token for :func:`reset_subagent_depth`."""
    return _current_depth.set(depth)


def reset_subagent_depth(token: contextvars.Token[int]) -> None:
    """Restore the previous subagent recursion depth bound by :func:`bind_subagent_depth`."""
    _current_depth.reset(token)


def current_depth_token() -> contextvars.Token[int]:
    """Capture the current depth as a token; reset later with :func:`reset_subagent_depth`.

    Equivalent to ``bind_subagent_depth(get_current_subagent_depth())`` — useful
    when a caller wants to snapshot-and-restore rather than increment.
    """
    return _current_depth.set(_current_depth.get())


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float  # time.monotonic()
    # initializing | awaiting_tools | tool_started | tool_blocked |
    # tool_completed | tools_completed | final_response | done | error
    phase: str = "initializing"
    iteration: int = 0
    tool_events: list = field(default_factory=list)  # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)  # token usage
    stop_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SubagentDefinition:
    """A declarative subagent definition parsed from a .md file."""

    name: str
    description: str
    system_prompt: str
    model: str | None = None
    tools: list[str] | None = None  # None = all tools
    avatar: str | None = None  # Optional emoji avatar (e.g. "🔍")
    file_path: Path | None = None

    def to_summary(self) -> str:
        """One-line summary for system prompt injection."""
        tools_hint = ""
        if self.tools is not None:
            tools_hint = f" (tools: {', '.join(self.tools)})"
        return f"- {self.name}: {self.description}{tools_hint}"


class SubagentRegistry:
    """Loads subagent definitions from agents/ directory (.md files)."""

    _FRONTMATTER_RE = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n(.*)$",
        re.DOTALL,
    )

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.agents_dir = workspace / "agents"
        self._definitions: dict[str, SubagentDefinition] = {}

    def load(self) -> int:
        """Scan agents/ dir and load all .md files. Returns count loaded."""
        self._definitions.clear()
        if not self.agents_dir.is_dir():
            return 0
        count = 0
        for md_file in sorted(self.agents_dir.glob("*.md")):
            try:
                defn = self._parse_file(md_file)
                if defn:
                    # First loaded wins (TRAE-style: project-level could override)
                    if defn.name not in self._definitions:
                        self._definitions[defn.name] = defn
                        count += 1
                    else:
                        logger.debug(
                            "Subagent '{}' already defined, skipping {}", defn.name, md_file
                        )
            except Exception:
                logger.exception("Failed to parse subagent file: {}", md_file)
        if count:
            logger.info("Loaded {} subagent definitions from {}", count, self.agents_dir)
        return count

    def _parse_file(self, path: Path) -> SubagentDefinition | None:
        """Parse a .md file with YAML frontmatter."""
        text = path.read_text(encoding="utf-8")
        match = self._FRONTMATTER_RE.match(text)
        if not match:
            logger.warning("Subagent file {} has no frontmatter, skipping", path)
            return None
        fm_text, body = match.group(1), match.group(2)
        try:
            parsed = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError as e:
            logger.warning("Subagent file {} has invalid YAML frontmatter: {}", path, e)
            return None
        if not isinstance(parsed, dict):
            logger.warning("Subagent file {} frontmatter is not a mapping, skipping", path)
            return None
        # 所有值统一转成字符串以保持向后兼容(name/description/model/avatar 均为标量)
        meta: dict[str, str] = {}
        for k, v in parsed.items():
            meta[str(k)] = "" if v is None else str(v).strip() if isinstance(v, str) else str(v)

        name = meta.get("name", "").strip()
        description = meta.get("description", "").strip()
        if not name or not description:
            logger.warning("Subagent file {} missing name/description, skipping", path)
            return None
        model = meta.get("model", "").strip() or None

        # tools 字段支持三种形式:
        #   tools: read_file, edit_file        → 逗号分隔字符串
        #   tools: "" / tools: '' / tools:     → 空列表(明确禁用所有工具)
        #   tools: [read_file, edit_file]      → YAML 列表(已由 safe_load 转为 list)
        #   缺省                                → None 表示继承全部工具
        raw_tools = parsed.get("tools")
        tools: list[str] | None
        if raw_tools is None:
            tools = None
        elif isinstance(raw_tools, list):
            tools = [str(t).strip() for t in raw_tools if str(t).strip()]
        else:
            tools_str = str(raw_tools).strip()
            if not tools_str:
                tools = []
            else:
                tools = [t.strip() for t in tools_str.split(",") if t.strip()]

        avatar = meta.get("avatar", "").strip() or None
        return SubagentDefinition(
            name=name,
            description=description,
            system_prompt=body.strip(),
            model=model,
            tools=tools,
            avatar=avatar,
            file_path=path,
        )

    def get(self, name: str) -> SubagentDefinition | None:
        return self._definitions.get(name)

    def list_all(self) -> list[SubagentDefinition]:
        return list(self._definitions.values())

    def build_prompt_section(self) -> str:
        """Build the 'Available Subagents' section for the main agent's system prompt."""
        if not self._definitions:
            return ""
        lines = [
            "# Available Subagents",
            "",
            "You can delegate tasks to specialized subagents via the `delegate` tool.",
            "The LLM autonomously decides when to delegate based on the descriptions below.",
            "",
        ]
        for defn in self._definitions.values():
            lines.append(defn.to_summary())
        lines.append("")
        lines.append('Usage: delegate(subagent="<name>", task="<task description>")')
        return "\n".join(lines)
