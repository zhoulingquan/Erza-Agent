"""Small helpers for passing the active LLM provider/model together.

Provider-runtime accessor: the active ``LLMProvider`` + model pair that the
agent runner and WebUI title generation capture for a turn.  Moved here from
``erza.utils.llm_runtime`` (which now re-exports) since this is provider
domain, not generic utils.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from erza.providers.base import LLMProvider


@dataclass(frozen=True)
class LLMRuntime:
    provider: LLMProvider
    model: str


LLMRuntimeResolver = Callable[[], LLMRuntime]


def static_llm_runtime(provider: LLMProvider, model: str) -> LLMRuntimeResolver:
    runtime = LLMRuntime(provider=provider, model=model)
    return lambda: runtime
