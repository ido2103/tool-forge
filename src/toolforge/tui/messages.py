"""Typed Textual messages — the bridge's whole vocabulary.

Every signal from the agent side (streaming deltas, hook events) crosses into
the UI as one of these, posted to the app and handled on the main message pump.
This keeps widget updates ordered and single-threaded no matter which task the
originating callback ran in, and it is the exact seam a future web host would
replace with its own transport.
"""

from __future__ import annotations

from textual.message import Message


class ThinkingDelta(Message):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class TextDelta(Message):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class TurnFinished(Message):
    """Posted by the turn worker instead of touching widgets directly: routed
    through the pump, it is guaranteed to arrive *after* every delta the turn
    produced — finishing synchronously in the worker would race them."""

    def __init__(self, final_text: str, error: str | None = None) -> None:
        self.final_text = final_text
        self.error = error
        super().__init__()


class ToolStarted(Message):
    def __init__(self, tool_name: str, call_id: str, preview: str, component: str) -> None:
        self.tool_name = tool_name
        self.call_id = call_id
        self.preview = preview
        self.component = component
        super().__init__()


class ToolFinished(Message):
    def __init__(
        self, tool_name: str, call_id: str, is_error: bool, latency_ms: int, component: str
    ) -> None:
        self.tool_name = tool_name
        self.call_id = call_id
        self.is_error = is_error
        self.latency_ms = latency_ms
        self.component = component
        super().__init__()


class ForgePhase(Message):
    def __init__(self, tool: str, phase: str, extra: dict[str, object]) -> None:
        self.tool = tool
        self.phase = phase
        self.extra = extra
        super().__init__()
