"""Transcription credential resolution helpers (pure; no Config dependency).

These functions replace the legacy ``ChannelManager`` reads of
``config.providers.groq.*``.  They have no runtime import from ``erza.config``
(not even under TYPE_CHECKING); callers resolve the groq values in the
composition root and inject them here.

Semantics mirror the old manager helpers line-for-line:

- ``openai`` reads the key/base from the environment
  (``OPENAI_API_KEY`` / ``OPENAI_API_BASE``);
- the groq branch returns the injected value, normalizing a missing
  (``None``) value to ``""``.  The legacy code wrapped the config read in
  ``try/except AttributeError`` because tests constructed managers over
  partial configs; the ``None``-guard is the equivalent tolerance here.
"""

from __future__ import annotations

import os


def resolve_transcription_key(provider: str, groq_api_key: str | None) -> str:
    """Pick the API key for the configured transcription provider."""
    if provider == "openai":
        # OpenAI Whisper transcription reads the key directly from env.
        # (openai LLM provider 已从 ProvidersConfig 移除，不再作为字段存在。)
        return os.environ.get("OPENAI_API_KEY", "") or ""
    return groq_api_key or ""


def resolve_transcription_base(provider: str, groq_api_base: str | None) -> str:
    """Pick the API base URL for the configured transcription provider."""
    if provider == "openai":
        # OpenAI Whisper 默认走官方端点；用户可设 OPENAI_API_BASE 覆盖。
        return os.environ.get("OPENAI_API_BASE", "") or ""
    return groq_api_base or ""
