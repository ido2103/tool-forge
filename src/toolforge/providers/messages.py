"""Toolforge-canonical conversation message types.

Pydantic discriminated union by content-block ``type``. Each provider adapter
translates this canonical shape to/from its native wire format, so the
orchestrator and forge loops never touch provider-specific dicts.

Ported from Zeemon ``providers/messages.py``; dropped Zeemon's transport
metadata (``source`` / ``external_id``) and the Responses-API-only
``OpaqueReasoningBlock``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBlock(_Block):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(_Block):
    type: Literal["tool_use"] = "tool_use"
    id: str  # Anthropic-style "toolu_..." id; preserved across providers
    name: str
    input: dict[str, Any]


class ToolResultBlock(_Block):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[dict[str, Any]]
    is_error: bool = False


class ThinkingBlock(_Block):
    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str | None = None  # absent on synthesized turns / non-Anthropic providers


class RedactedThinkingBlock(_Block):
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


class DocumentBlock(_Block):
    type: Literal["document"] = "document"
    title: str
    content: str
    media_type: str = "text/plain"
    source_type: Literal["text", "base64"] = "text"


class ImageBlock(_Block):
    type: Literal["image"] = "image"
    data: str  # base64-encoded bytes
    media_type: str  # "image/jpeg", "image/png", etc.


ContentBlock = Annotated[
    TextBlock
    | ToolUseBlock
    | ToolResultBlock
    | ThinkingBlock
    | RedactedThinkingBlock
    | DocumentBlock
    | ImageBlock,
    Field(discriminator="type"),
]


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: list[ContentBlock]

    ts: AwareDatetime

    # -- convenience accessors ------------------------------------------------

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_use_blocks(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    # -- provider / model metadata --------------------------------------------

    provider: Literal["anthropic", "openai"] | None = None
    auth_mode: Literal["api_key", "oauth"] | None = None
    model: str | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    latency_ms: int | None = None
