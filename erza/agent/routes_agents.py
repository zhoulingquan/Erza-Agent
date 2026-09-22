"""Backward-compatible re-export of the subagent route handlers.

The stateless ``router`` implementation moved to
:mod:`erza.contracts.routes_agents` (the shared agent/transport contract
layer) so the WebSocket channel's ``/api/agents*`` handlers no longer import
``erza.agent``. This module keeps the legacy import path working; import
from ``erza.contracts.routes_agents`` in new code.
"""

from __future__ import annotations

from erza.contracts.routes_agents import router

__all__ = ("router",)
