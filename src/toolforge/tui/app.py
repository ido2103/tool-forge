"""The Toolforge Textual app — chat surface over ``bootstrap.build_host``.

``toolforge-tui`` is the console script. Configuration errors are caught in
:func:`main` *before* ``App.run()`` so they print to a normal terminal instead
of dying inside the alternate screen; everything after boot renders in-app.

Concurrency: Textual owns the asyncio loop. The sandbox boots in a mount-time
worker (first paint is never blocked); each turn runs in an exclusive worker
awaiting ``Orchestrator.run``. Esc requests a graceful stop via the loop's own
cancel-event machinery — the Textual worker itself is never cancelled, so the
turn ends cleanly ("Stopping.") with history intact.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input

from toolforge.config import (
    AnthropicSettings,
    OrchestratorSettings,
    SandboxSettings,
    TestAuthorSettings,
    WorkerSettings,
)
from toolforge.orchestrator.bootstrap import Host, build_host
from toolforge.providers import Message
from toolforge.tui.bridge import attach_hooks, make_delta_callbacks
from toolforge.tui.messages import (
    ForgePhase,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from toolforge.tui.widgets import ChatLog, ForgePanel, ToolActivity


class ToolforgeApp(App[None]):
    """Chat pane + prompt over one `Host`; later slices add sidebar and modal."""

    CSS_PATH = "styles.tcss"
    TITLE = "toolforge"
    BINDINGS = [
        Binding("escape", "stop_turn", "Stop turn"),
        Binding("ctrl+n", "new_session", "New session"),
    ]

    def __init__(self, host: Host) -> None:
        super().__init__()
        self._host = host
        self._history: list[Message] = []
        self._turn_running = False
        self._booted = False

    # ── layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield ChatLog(id="chat")
            with Vertical(id="side"):
                yield ToolActivity(id="activity")
                yield ForgePanel(id="forge", classes="hidden")
        yield Input(placeholder="type a task — /new /reset /quit · Esc stops a turn", id="prompt")
        yield Footer()

    @property
    def chat(self) -> ChatLog:
        return self.query_one(ChatLog)

    @property
    def prompt(self) -> Input:
        return self.query_one("#prompt", Input)

    @property
    def activity(self) -> ToolActivity:
        return self.query_one("#activity", ToolActivity)

    @property
    def forge_panel(self) -> ForgePanel:
        return self.query_one("#forge", ForgePanel)

    @property
    def turn_running(self) -> bool:
        return self._turn_running

    # ── boot ────────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        attach_hooks(self, self._host.hooks)
        self.sub_title = f"{self._host.model} · sandbox starting…"
        self.prompt.disabled = True
        self.run_worker(self._boot(), group="boot")

    async def _boot(self) -> None:
        for warning in self._host.tool_store_warnings:
            self.chat.add_error(f"[tool store: {warning}]")
        if self._host.loaded_tools:
            names = ", ".join(self._host.loaded_tools)
            self.chat.add_system(f"(loaded {len(self._host.loaded_tools)} forged tool(s): {names})")
        try:
            await self._host.sandbox.start()
        except RuntimeError as exc:
            self.sub_title = "sandbox FAILED"
            self.chat.add_error(f"Sandbox startup failed: {exc}\nIs Docker running?")
            return
        self._booted = True
        self.sub_title = f"{self._host.model} · sandbox ready"
        self.prompt.disabled = False
        self.prompt.focus()

    # ── input dispatch ──────────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text:
            await self.handle_submit(text)

    async def handle_submit(self, text: str) -> None:
        """Dispatch one line of user input: slash command or a task turn."""
        if text in ("/quit", "/exit"):
            self.exit()
            return
        if text.startswith("/") and self._turn_running:
            self.chat.add_system("(a turn is running — Esc to stop it first)")
            return
        if text == "/new":
            self._history.clear()
            self.chat.add_system("(history cleared)")
            return
        if text == "/reset":
            self.run_worker(self._reset(), group="boot", exclusive=True)
            return
        if text.startswith("/"):
            self.chat.add_system(f"(unknown command {text!r} — /new /reset /quit)")
            return
        if not self._booted:
            self.chat.add_system("(sandbox is not ready — wait for boot or fix Docker and /reset)")
            return
        if self._turn_running:
            self.chat.add_system("(a turn is already running — Esc stops it)")
            return
        self.run_worker(self._run_turn(text), group="turn", exclusive=True)

    async def _reset(self) -> None:
        """Parity with the REPL's /reset: clear session state, recycle the container."""
        self._history.clear()
        # Unpromoted candidates die with the session state; registered tools
        # survive (they live on disk and in the registry).
        self._host.candidates.clear()
        self._booted = False
        self.prompt.disabled = True
        self.sub_title = "sandbox restarting…"
        self._host.sandbox.teardown()
        try:
            await self._host.sandbox.start()
        except RuntimeError as exc:
            self.sub_title = "sandbox FAILED"
            self.chat.add_error(f"[sandbox restart failed: {exc}]")
            return
        self._booted = True
        self.sub_title = f"{self._host.model} · sandbox ready"
        self.prompt.disabled = False
        self.chat.add_system("(history cleared, candidates dropped, container recycled)")
        self.prompt.focus()

    # ── the turn ────────────────────────────────────────────────────────────

    async def _run_turn(self, text: str) -> None:
        self._turn_running = True
        self.prompt.disabled = True
        self.chat.add_user(text)
        self.chat.start_stream()
        on_thinking, on_text = make_delta_callbacks(self)
        try:
            final = await self._host.orchestrator.run(
                text,
                self._history,
                system_prompt=self._host.system_prompt,
                on_thinking_delta=on_thinking,
                on_text_delta=on_text,
            )
        except Exception as exc:  # provider/auth errors etc. — report, don't crash the app
            self.post_message(TurnFinished("", error=f"[error: {exc!r}]"))
        else:
            self.post_message(TurnFinished(final))

    # ── streaming messages ──────────────────────────────────────────────────

    def on_thinking_delta(self, message: ThinkingDelta) -> None:
        self.chat.append_thinking(message.text)

    def on_text_delta(self, message: TextDelta) -> None:
        self.chat.append_answer(message.text)

    def on_turn_finished(self, message: TurnFinished) -> None:
        self.chat.end_stream(message.final_text)
        if message.error is not None:
            self.chat.add_error(message.error)
        self._turn_running = False
        self.prompt.disabled = False
        self.prompt.focus()

    # ── tool activity + forge narration ─────────────────────────────────────

    def on_tool_started(self, message: ToolStarted) -> None:
        if message.component == "forge_worker":
            label = f"→ {message.tool_name}" + (f": {message.preview}" if message.preview else "")
            self.forge_panel.add_worker_event(label)
            return
        self.activity.start_call(message.call_id, message.tool_name, message.preview)
        if message.tool_name == "forge_tool":
            self.forge_panel.begin(message.call_id)

    def on_tool_finished(self, message: ToolFinished) -> None:
        if message.component == "forge_worker":
            return
        self.activity.finish_call(message.call_id, message.is_error, message.latency_ms)
        if message.tool_name == "forge_tool":
            self.forge_panel.finish(message.call_id, message.is_error)

    def on_forge_phase(self, message: ForgePhase) -> None:
        self.forge_panel.set_phase(message.tool, message.phase, message.extra)

    # ── actions ─────────────────────────────────────────────────────────────

    def action_stop_turn(self) -> None:
        if self._turn_running:
            self._host.orchestrator.request_stop()
            self.chat.add_system("(stop requested…)")

    def action_new_session(self) -> None:
        if self._turn_running:
            self.chat.add_system("(a turn is running — Esc to stop it first)")
            return
        self._history.clear()
        self.chat.add_system("(history cleared)")


def _default_host() -> Host:
    return build_host(
        AnthropicSettings(),
        OrchestratorSettings(),
        SandboxSettings(),
        WorkerSettings(),
        TestAuthorSettings(),
        ask_user=None,  # the modal callback lands in the ask_user slice
    )


def main() -> None:
    try:
        host = _default_host()
    except ValidationError as exc:
        # Almost always missing/invalid credentials or sandbox config.
        print(
            f"Configuration error:\n{exc}\n\n"
            "Copy .env.example to .env and fill in your credentials "
            "(see TOOLFORGE_ANTHROPIC_* / ANTHROPIC_API_KEY).",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except ValueError as exc:
        # e.g. the boot-time cross-model separation check.
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        ToolforgeApp(host).run()
    except asyncio.CancelledError:  # a hard teardown mid-turn; nothing to save
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
