"""Shared loop-test harness: a scriptable fake provider + message/tool factories.

Imported by both ``conftest.py`` (for fixtures) and the loop tests. Kept out of
``conftest.py`` itself so tests can import the classes/factories directly (pytest
puts this directory on ``sys.path`` under its default prepend import mode).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from toolforge.providers import (
    AuthMode,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from toolforge.registry import RegisteredTool, ToolContext, ToolResult

# ── message factories ────────────────────────────────────────────────────────


def assistant_text(text: str, *, stop_reason: str = "end_turn") -> Message:
    return Message(
        role="assistant",
        content=[TextBlock(text=text)],
        ts=datetime.now(tz=UTC),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def assistant_tool_use(
    *calls: tuple[str, str, dict[str, Any]],
    text: str = "",
    stop_reason: str = "tool_use",
) -> Message:
    """Build an assistant tool_use message from ``(id, name, input)`` tuples."""
    content: list[Any] = []
    if text:
        content.append(TextBlock(text=text))
    content.extend(ToolUseBlock(id=cid, name=name, input=inp) for cid, name, inp in calls)
    return Message(
        role="assistant",
        content=content,
        ts=datetime.now(tz=UTC),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def make_tool(
    name: str,
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]],
    *,
    trust: str = "TRUSTED",
    serial_group: str | None = None,
) -> RegisteredTool:
    return RegisteredTool(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        trust=trust,  # type: ignore[arg-type]
        serial_group=serial_group,
    )


# ── fake provider ────────────────────────────────────────────────────────────


class FakeProviderClient:
    """Replays a scripted list of responses; records every send() call."""

    name = "fake"
    auth_mode = AuthMode.API_KEY

    def __init__(self, script: list[Message | Exception]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        messages: list[Message],
        system: str,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int,
        cancel_event: Any = None,
        turn_id: Any = None,
        component: str = "orchestrator",
        extra: dict[str, Any] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Message:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "system": system,
                "max_tokens": max_tokens,
                "model": model,
                "component": component,
            }
        )
        if not self._script:
            raise AssertionError("FakeProviderClient script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, **kwargs: Any) -> Any:  # pragma: no cover - loop uses send()
        raise NotImplementedError
