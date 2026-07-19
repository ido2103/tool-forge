"""TUI widgets: the chat log, the tool-activity sidebar, and the forge panel.

ChatLog buffers token deltas in plain strings and flushes on a ~20 Hz timer —
deltas arrive far faster than a layout pass is worth. ToolActivity shows one
row per orchestrator tool call. ForgePanel is the build's live narration: a
forge occupies the sandbox for minutes, and this panel (phase line + elapsed
clock + the worker's own tool calls) is what makes that time legible.
"""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

_FLUSH_INTERVAL = 0.05


class ChatLog(VerticalScroll):
    """Scrolling column of chat messages; owns the streaming tail."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual's own kwarg name
        super().__init__(id=id)
        self._thinking_buf = ""
        self._answer_buf = ""
        self._dirty = False
        self._tail_thinking: Static | None = None
        self._tail_answer: Static | None = None

    def on_mount(self) -> None:
        self.set_interval(_FLUSH_INTERVAL, self._flush)

    # ── static messages ─────────────────────────────────────────────────────

    def add_user(self, text: str) -> None:
        self._add(f"» {text}", "user")

    def add_system(self, text: str) -> None:
        self._add(text, "system")

    def add_error(self, text: str) -> None:
        self._add(text, "error")

    def _add(self, text: str, kind: str) -> None:
        # markup=False throughout: chat text is full of literal brackets
        # ([error: …], [tool store: …]) that must never parse as style tags.
        self.mount(Static(text, classes=f"msg {kind}", markup=False))
        self.scroll_end(animate=False)

    # ── the streaming tail ──────────────────────────────────────────────────

    def start_stream(self) -> None:
        self._thinking_buf = ""
        self._answer_buf = ""
        self._tail_thinking = Static("", classes="msg thinking", markup=False)
        self._tail_answer = Static("", classes="msg assistant", markup=False)
        self.mount(self._tail_thinking, self._tail_answer)

    def append_thinking(self, text: str) -> None:
        self._thinking_buf += text
        self._dirty = True

    def append_answer(self, text: str) -> None:
        self._answer_buf += text
        self._dirty = True

    def end_stream(self, final_text: str) -> None:
        """Freeze the tail. *final_text* is authoritative (the loop's return
        value) — a turn that ends via a canned path ("Stopping.", a refusal)
        streamed nothing, and this is where that text still gets rendered."""
        if final_text and not self._answer_buf:
            self._answer_buf = final_text
        self._flush(force=True)
        for widget in (self._tail_thinking, self._tail_answer):
            if widget is not None and not str(widget.content):
                widget.remove()
        self._tail_thinking = None
        self._tail_answer = None

    @property
    def answer_text(self) -> str:
        """Current streamed answer text (exposed for tests)."""
        return self._answer_buf

    def _flush(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        self._dirty = False
        if self._tail_thinking is not None:
            self._tail_thinking.update(self._thinking_buf)
        if self._tail_answer is not None:
            self._tail_answer.update(self._answer_buf)
        self.scroll_end(animate=False)


class ToolActivity(VerticalScroll):
    """One row per orchestrator tool call: `→ name: preview`, then ✓/✗ + latency."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual's kwarg name
        super().__init__(id=id)
        self._rows: dict[str, tuple[Static, str]] = {}

    def start_call(self, call_id: str, tool_name: str, preview: str) -> None:
        label = f"→ {tool_name}" + (f": {preview}" if preview else "")
        row = Static(label, classes="tool-row running", markup=False)
        self._rows[call_id] = (row, label)
        self.mount(row)
        self.scroll_end(animate=False)

    def finish_call(self, call_id: str, is_error: bool, latency_ms: int) -> None:
        entry = self._rows.pop(call_id, None)
        if entry is None:
            return
        row, label = entry
        mark = "✗" if is_error else "✓"
        row.update(f"{mark} {label[2:]} ({latency_ms}ms)")
        row.remove_class("running")
        row.add_class("error" if is_error else "done")


_PHASE_TEXT = {
    "authoring_tests": "authoring adversarial tests…",
    "building": "worker starting…",
    "verifying": "verifying in a fresh container…",
    "candidate_ready": "✓ candidate ready",
    "failed": "✗ build failed",
}


class ForgePanel(Vertical):
    """Live narration of one forge build; hidden until a forge_tool call starts."""

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 - Textual's own kwarg name
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._active_call: str | None = None
        self._tool = ""
        self._phase_text = ""
        self._started = 0.0
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static("", id="forge-status", markup=False)
        yield VerticalScroll(id="forge-feed")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    @property
    def status_text(self) -> str:
        return str(self.query_one("#forge-status", Static).content)

    def begin(self, call_id: str) -> None:
        self._active_call = call_id
        self._tool = ""
        self._phase_text = "starting…"
        self._started = time.monotonic()
        self._finished = False
        self.query_one("#forge-feed", VerticalScroll).remove_children()
        self.remove_class("hidden")
        self._render_status()

    def set_phase(self, tool: str, phase: str, extra: dict[str, object]) -> None:
        self._tool = tool
        if phase == "tests_ready":
            self._phase_text = f"{extra.get('test_count')} adversarial tests ready (red)"
        elif phase == "attempt":
            self._phase_text = f"worker attempt {extra.get('attempt')}/{extra.get('max_attempts')}"
        elif phase == "attempt_failed":
            tampered = extra.get("tampered") or []
            note = " — tampering detected!" if tampered else ""
            self._phase_text = f"attempt failed{note}"
        elif phase == "candidate_ready":
            self._phase_text = f"✓ candidate ready ({extra.get('attempts')} attempt(s))"
        else:
            self._phase_text = _PHASE_TEXT.get(phase, phase)
        self._render_status()

    def add_worker_event(self, text: str) -> None:
        feed = self.query_one("#forge-feed", VerticalScroll)
        feed.mount(Static(text, classes="feed-row", markup=False))
        feed.scroll_end(animate=False)

    def finish(self, call_id: str, is_error: bool) -> None:
        if call_id != self._active_call:
            return
        self._active_call = None
        self._finished = True
        if is_error and not self._phase_text.startswith("✗"):
            self._phase_text = "✗ build failed"
        self._render_status()

    def _tick(self) -> None:
        if self._active_call is not None:
            self._render_status()

    def _render_status(self) -> None:
        elapsed = int(time.monotonic() - self._started) if self._started else 0
        clock = f"{elapsed // 60}:{elapsed % 60:02d}"
        name = f"forge[{self._tool}]" if self._tool else "forge"
        suffix = "" if self._finished else f" · {clock}"
        self.query_one("#forge-status", Static).update(f"{name}: {self._phase_text}{suffix}")
