"""Re-export shim for :mod:`erza.providers.runtime`.

The LLM-provider runtime accessors canonicalized in
``erza/providers/runtime.py`` (provider domain); this module keeps old
imports working.
"""

from __future__ import annotations

from erza.providers.runtime import (
    LLMRuntime,
    LLMRuntimeResolver,
    static_llm_runtime,
)

__all__ = ["LLMRuntime", "LLMRuntimeResolver", "static_llm_runtime"]
