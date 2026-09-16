"""Per-tool configuration classes.

These classes used to live next to their tool implementations
(``erza.tools.shell.ExecToolConfig`` etc.), which forced
``erza.config.schema`` to import ``erza.tools`` at runtime to resolve the
``ToolsConfig`` fields — a base→capability dependency inversion that only
worked through eager/lazy ``model_rebuild()`` gymnastics.

They are pure declarative pydantic models, so their canonical home is
here in the config layer:

- ``erza.config.schema`` imports them at module level (no cycle: this
  module depends only on :mod:`erza.config.base` + pydantic);
- tools reference their own config via ``from erza.config.tool_configs
  import ...`` (tools→config is the allowed direction);
- ``erza.config.schema`` re-exports them, so existing
  ``from erza.config.schema import ExecToolConfig`` imports keep working.

Moved verbatim from ``erza.tools.{exec_session,self,shell,web}``; import
from here in new code.
"""

from __future__ import annotations

from pydantic import Field

from erza.config.base import Base

__all__ = [
    "ExecSessionToolConfig",
    "ExecToolConfig",
    "MyToolConfig",
    "WebFetchConfig",
    "WebToolsConfig",
]


class ExecSessionToolConfig(Base):
    """Configuration for the interactive exec-session tools.

    ``enabled`` gates registration of ``write_stdin`` and
    ``list_exec_sessions``. It defaults to ``True`` so existing setups keep
    their current tool set; set it to ``false`` to omit both tools from the
    registry (the main ``exec`` tool is unaffected).
    """

    enabled: bool = True


class MyToolConfig(Base):
    """Self-inspection tool configuration."""

    enable: bool = True
    allow_set: bool = False


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True
    timeout: int = Field(
        default=60, ge=0
    )  # Hard timeout (s); 0 = no limit. Not capped by the per-call max.
    path_append: str = ""
    sandbox: str = ""
    # 沙箱不可用时(如 Windows 平台)拒绝执行命令,而非静默降级为无沙箱
    # 运行。默认 False 保持向后兼容(仅记录告警);生产环境建议开启。
    sandbox_required: bool = False
    # 沙箱是否启用网络隔离(--unshare-net)。默认 False,因为多数命令需要联网;
    # 仅在确需网络隔离的工作流中显式开启。
    unshare_net: bool = False
    allowed_env_keys: list[str] = Field(default_factory=list)
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)


class WebFetchConfig(Base):
    """Web fetch tool configuration.

    use_jina_reader 字段保留向后兼容,但 Jina Reader 现已自动化:
    首次调用时懒探测可达性,不可达则自动降级到 readability,无需手动开关。
    """

    use_jina_reader: bool = True  # 保留字段,Jina 已自动化(懒探测+熔断器)


class WebToolsConfig(Base):
    """Web tools configuration."""

    enable: bool = True
    proxy: str | None = None
    user_agent: str | None = None
    fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)
