"""Channel configuration schema (extracted from erza.config.schema)."""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field

from erza.config.base import Base


class ChannelsConfig(Base):
    """Configuration for chat channels.

    QwenPaw-style: built-in channel configs are declared as explicit fields
    (each channel lives in ``erza/channels/<name>/`` and parses its
    own config dict in ``__init__``). Plugin channel configs are still
    stored via ``extra="allow"`` (``__pydantic_extra__`` dict).
    Per-channel ``"streaming": true`` enables streaming output (requires
    send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    show_reasoning: bool = True  # surface model reasoning when channel implements it
    extract_document_text: bool = (
        True  # extract text from document attachments before sending to the model
    )
    send_max_retries: int = Field(
        default=3, ge=0, le=10
    )  # Max delivery attempts (initial send included)
    send_timeout_s: float = Field(
        default=30.0, ge=0
    )  # Per-attempt delivery timeout; 0 disables. Guards the shared outbound
    # dispatcher against a stalled channel (e.g. a websocket client that stops
    # reading) freezing delivery for every other channel.
    transcription_provider: str = "groq"  # Voice transcription backend: "groq" or "openai"
    transcription_language: str | None = Field(
        default=None, pattern=r"^[a-z]{2,3}$"
    )  # Optional ISO-639-1 hint for audio transcription

    # Built-in channel configs (QwenPaw-style explicit fields). None = not
    # configured / disabled; dict = parsed by the channel's own Config class
    # in __init__. Plugin channels still use the extras dict.
    feishu: dict[str, Any] | None = None
    dingtalk: dict[str, Any] | None = None
    qq: dict[str, Any] | None = None
    wecom: dict[str, Any] | None = None
    weixin: dict[str, Any] | None = None
    websocket: dict[str, Any] | None = None
