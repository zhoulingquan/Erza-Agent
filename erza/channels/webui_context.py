"""WebUI wiring context for the channel layer.

All WebUI-facing collaborators the WebSocket channel needs at construction
time are bundled into a single :class:`WebUIContext` so ``ChannelManager``
exposes one ``context`` parameter instead of a long tail of ``webui_*``
keyword arguments.

``ChannelManager`` still accepts the legacy ``webui_*`` keyword arguments
for backward compatibility: when a legacy keyword is explicitly passed it
overrides the corresponding context field and emits a ``DeprecationWarning``.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

__all__ = ["WebUIContext"]


@dataclass(slots=True)
class WebUIContext:
    """Bundled WebUI-related wiring for the WebSocket channel.

    Fields mirror the legacy ``ChannelManager(..., webui_*)`` keyword
    arguments (see ``erza/composition/gateway.py`` for the composition-root
    wiring): ``runtime_model_name`` ⇄ ``webui_runtime_model_name``,
    ``static_dist`` ⇄ ``webui_static_dist``, etc.
    """

    runtime_model_name: Callable[[], str | None] | None = None
    static_dist: bool = True
    runtime_surface: str = "browser"
    runtime_capabilities: dict[str, Any] | None = None
    provider_loader: Callable[[], Any] | None = None
    cron_reloader: Callable[[], None] | None = None
    agent_model_refresher: Callable[[], None] | None = None
    cron_service: Any = None
    tool_registry: Any = None
    mcp_reloader: Callable | None = None
    mcp_connector: Callable | None = None


_UNSET = object()

# legacy keyword name → WebUIContext field name
_LEGACY_KWARGS: tuple[tuple[str, str], ...] = (
    ("webui_runtime_model_name", "runtime_model_name"),
    ("webui_static_dist", "static_dist"),
    ("webui_runtime_surface", "runtime_surface"),
    ("webui_runtime_capabilities", "runtime_capabilities"),
    ("webui_provider_loader", "provider_loader"),
    ("webui_cron_reloader", "cron_reloader"),
    ("webui_agent_model_refresher", "agent_model_refresher"),
    ("webui_cron_service", "cron_service"),
    ("webui_tool_registry", "tool_registry"),
    ("webui_mcp_reloader", "mcp_reloader"),
    ("webui_mcp_connector", "mcp_connector"),
)


def resolve_webui_context(
    context: WebUIContext | None,
    legacy: dict[str, Any],
) -> WebUIContext:
    """Merge a ``WebUIContext`` with legacy ``webui_*`` keyword overrides.

    Returns the effective context. Legacy kwargs that were *explicitly
    provided* by the caller (their value is not the internal :data:`_UNSET`
    sentinel — note ``None`` is a legitimate explicit value) override the
    context field, and each non-empty override batch triggers a single
    ``DeprecationWarning``.
    """
    ctx = context if context is not None else WebUIContext()
    overrides: dict[str, Any] = {}
    for kw, field in _LEGACY_KWARGS:
        value = legacy.get(kw, _UNSET)
        if value is not _UNSET:
            overrides[field] = value
    if overrides:
        warnings.warn(
            "ChannelManager webui_* keyword arguments are deprecated; "
            "pass a WebUIContext via the `context=` parameter instead. "
            f"Deprecated argument(s): {sorted(overrides)}",
            DeprecationWarning,
            stacklevel=3,
        )
        ctx = replace(ctx, **overrides)
    return ctx
