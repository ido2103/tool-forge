"""The ``ask_user`` modal — the TUI's answer channel for mid-turn questions.

Renders the validated :class:`AskUserRequest`: question, the constraints behind
it, one button per option (recommended styled primary, label returned
*verbatim* — the handler's label check depends on it), and a free-text input.
There is no escape-dismiss: the only way past the question without answering is
cancelling the turn, mirroring the REPL's no-silent-default rule.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from toolforge.orchestrator.ask_user import AskUserRequest


class AskUserScreen(ModalScreen[str]):
    """Dismisses with the chosen option's verbatim label or the typed text."""

    def __init__(self, request: AskUserRequest) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-dialog"):
            yield Static(self._request.question, id="ask-question", markup=False)
            yield Static(self._request.context, id="ask-context", markup=False)
            for i, opt in enumerate(self._request.options):
                suffix = " (recommended)" if opt.recommended else ""
                yield Button(
                    f"{opt.label}{suffix}",
                    id=f"opt-{i}",
                    variant="primary" if opt.recommended else "default",
                )
                yield Static(opt.description, classes="ask-desc", markup=False)
            yield Input(placeholder="…or type your own answer", id="ask-free")

    def on_mount(self) -> None:
        # Focus the recommended option if there is one, else the first button.
        recommended = next((i for i, o in enumerate(self._request.options) if o.recommended), 0)
        self.query_one(f"#opt-{recommended}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        index = int(str(event.button.id).removeprefix("opt-"))
        self.dismiss(self._request.options[index].label)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # must not bubble into the app's prompt handler
        text = event.value.strip()
        if text:  # empty input never answers — parity with the REPL's re-prompt
            self.dismiss(text)
