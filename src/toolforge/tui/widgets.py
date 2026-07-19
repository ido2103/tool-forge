"""Chat-log widget: styled per-message statics with a buffered streaming tail.

Token deltas arrive far faster than a layout pass is worth; the log accumulates
them in plain-string buffers and a ~20 Hz timer flushes whatever changed. One
mutable "tail" pair (thinking + answer) exists only while a turn streams; on
turn end it is frozen into ordinary message widgets.
"""

from __future__ import annotations

from textual.containers import VerticalScroll
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
