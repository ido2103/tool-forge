"""Provider abstraction surface — Protocol, AuthMode, normalized events.

The orchestrator and forge loops call ``client.stream(...)`` / ``client.send(...)``
uniformly; each adapter implementation translates between toolforge canonical
messages (see ``providers/messages.py``) and its native SDK format.

Ported from Zeemon ``providers/base.py``. The protocol declares ``stream`` as a
non-async ``def`` returning ``AsyncIterator`` so async-generator implementations
conform structurally (Zeemon needed a ``cast`` because it declared ``async def``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from toolforge.providers.messages import Message

logger = logging.getLogger(__name__)


class AuthMode(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextDelta(_Event):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ThinkingDelta(_Event):
    type: Literal["thinking_delta"] = "thinking_delta"
    text: str


class ToolUseStart(_Event):
    type: Literal["tool_use_start"] = "tool_use_start"
    id: str
    name: str


class ToolUseDelta(_Event):
    type: Literal["tool_use_delta"] = "tool_use_delta"
    partial_json: str


class ToolUseEnd(_Event):
    type: Literal["tool_use_end"] = "tool_use_end"
    id: str


class MessageEnd(_Event):
    type: Literal["message_end"] = "message_end"
    message: Message


ProviderEvent = TextDelta | ThinkingDelta | ToolUseStart | ToolUseDelta | ToolUseEnd | MessageEnd


class ProviderClient(Protocol):
    """Uniform interface every provider adapter implements."""

    name: str  # "anthropic" | "openai"
    auth_mode: AuthMode

    def stream(
        self,
        *,
        messages: list[Message],
        system: str,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int,
        cancel_event: asyncio.Event | None = None,
        turn_id: UUID | None = None,
        component: str = "orchestrator",
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield normalized events; final MessageEnd carries the accumulated message.

        cancel_event: when set, the adapter aborts the underlying HTTP stream
        and raises asyncio.CancelledError.
        turn_id / component: forwarded to the usage hook (see ``providers/usage.py``).
        """
        ...

    async def send(
        self,
        *,
        messages: list[Message],
        system: str,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int,
        cancel_event: asyncio.Event | None = None,
        turn_id: UUID | None = None,
        component: str = "orchestrator",
        extra: dict[str, Any] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Message:
        """Drain ``stream(...)`` and return the final Message.

        *on_thinking_delta*: if provided, called with each ``ThinkingDelta``
        text chunk as it arrives from the stream.  Best-effort — exceptions
        from the callback are logged but do not abort the request.

        *on_text_delta*: same semantics for ``TextDelta`` events.
        """
        ...


async def drain_send(
    stream: AsyncIterator[ProviderEvent],
    *,
    on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
    on_text_delta: Callable[[str], Awaitable[None]] | None = None,
) -> Message:
    """Shared ``send()`` body: drain a provider stream to its final Message.

    Both Zeemon adapters duplicated this loop verbatim; extracted here once.
    Callback exceptions are logged and swallowed — they never abort the request.
    """
    async for ev in stream:
        if isinstance(ev, ThinkingDelta) and on_thinking_delta is not None:
            try:
                await on_thinking_delta(ev.text)
            except Exception:
                logger.debug("thinking_delta callback error", exc_info=True)
        elif isinstance(ev, TextDelta) and on_text_delta is not None:
            try:
                await on_text_delta(ev.text)
            except Exception:
                logger.debug("text_delta callback error", exc_info=True)
        if isinstance(ev, MessageEnd):
            return ev.message
    raise RuntimeError("provider stream ended without MessageEnd")
