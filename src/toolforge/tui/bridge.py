"""Agent-side callbacks → typed Textual messages posted to the app.

The orchestrator knows only its streaming-callback and hook contracts; this
module adapts both onto the app's message pump. Hook handlers run inside the
turn worker's task tree, so they never touch widgets directly — they post and
return.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from toolforge.orchestrator.hooks import HookEvent, HookManager
from toolforge.tui.messages import ForgePhase, TextDelta, ThinkingDelta, ToolFinished, ToolStarted

if TYPE_CHECKING:
    from toolforge.tui.app import ToolforgeApp

DeltaCallback = Callable[[str], Awaitable[None]]

_PREVIEW_LEN = 80


def tool_preview(inp: Any) -> str:
    """The same compact preview the REPL prints: command/question, truncated."""
    if not isinstance(inp, dict):
        return ""
    cmd = inp.get("command") or inp.get("question") or inp.get("name")
    if not isinstance(cmd, str):
        return ""
    return cmd if len(cmd) <= _PREVIEW_LEN else cmd[: _PREVIEW_LEN - 3] + "…"


def make_delta_callbacks(app: ToolforgeApp) -> tuple[DeltaCallback, DeltaCallback]:
    """(on_thinking_delta, on_text_delta) for ``Orchestrator.run``."""

    async def on_thinking(text: str) -> None:
        app.post_message(ThinkingDelta(text))

    async def on_text(text: str) -> None:
        app.post_message(TextDelta(text))

    return on_thinking, on_text


def attach_hooks(app: ToolforgeApp, hooks: HookManager) -> None:
    """Register the app's observers on the host HookManager.

    Called once at mount; registration after ``build_host`` is fine — the
    manager dispatches to whatever is registered at fire time.
    """

    def on_pre(**kw: Any) -> None:
        app.post_message(
            ToolStarted(
                tool_name=str(kw.get("tool_name")),
                call_id=str(kw.get("call_id")),
                preview=tool_preview(kw.get("input")),
                component=str(kw.get("component", "")),
            )
        )

    def on_post(**kw: Any) -> None:
        app.post_message(
            ToolFinished(
                tool_name=str(kw.get("tool_name")),
                call_id=str(kw.get("call_id")),
                is_error=bool(kw.get("is_error")),
                latency_ms=int(kw.get("latency_ms") or 0),
                component=str(kw.get("component", "")),
            )
        )

    def on_forge_phase(**kw: Any) -> None:
        extra = {k: v for k, v in kw.items() if k not in ("tool", "phase")}
        app.post_message(
            ForgePhase(tool=str(kw.get("tool")), phase=str(kw.get("phase")), extra=extra)
        )

    hooks.register(HookEvent.ON_TOOL_PRE_EXECUTE, on_pre)
    hooks.register(HookEvent.ON_TOOL_POST_EXECUTE, on_post)
    hooks.register(HookEvent.ON_FORGE_PHASE, on_forge_phase)
