"""Tool contract value types and the per-turn execution context.

Ported and trimmed from Zeemon ``core/types.py``. Toolforge drops the
session/config/pg coupling — a tool only needs a turn id and the ability to
register cancel-handlers so an in-flight tool can be aborted on emergency stop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

# Provenance of a tool's output. Hand-written seed tools are TRUSTED; forged
# tools (whose code the worker wrote) are UNVERIFIED and their output is wrapped
# so the model treats it as data, not instructions.
Trust = Literal["TRUSTED", "UNVERIFIED"]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_use_id: str
    content: str | list[dict[str, Any]]
    is_error: bool = False


class ToolContext:
    """Per-turn state threaded into every tool handler.

    ``turn_id`` is refreshed by the loop each turn. Cancel-handlers let a
    long-running tool (e.g. ``run_bash``) register cleanup that the loop fires
    when the user requests a stop mid-execution.
    """

    def __init__(self, *, turn_id: UUID | None = None) -> None:
        self.turn_id = turn_id
        self._cancel_handlers: list[Callable[[], Awaitable[None]]] = []

    def register_cancel_handler(self, handler: Callable[[], Awaitable[None]]) -> None:
        self._cancel_handlers.append(handler)

    def reset_cancel_handlers(self) -> None:
        """Clear handlers at the start of each tool batch."""
        self._cancel_handlers.clear()

    async def fire_cancel_handlers(self) -> None:
        """Run every registered cancel-handler, bounded to 2s total."""
        if not self._cancel_handlers:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    *(h() for h in self._cancel_handlers),
                    return_exceptions=True,
                ),
                timeout=2.0,
            )


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class RegisteredTool:
    """A tool the orchestrator can call: its wire schema + its handler + trust."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    trust: Trust = "TRUSTED"
    # Tools sharing a serial_group execute one at a time, in the order the model
    # emitted them; None (default) means the tool is safe to run concurrently.
    serial_group: str | None = None
    # Populated by ToolContext-free tools that want extra metadata later; unused v0.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def schema(self) -> dict[str, Any]:
        """The Anthropic-shape tool definition sent to the model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
