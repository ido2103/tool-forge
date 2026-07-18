"""The ``ask_user`` clarification tool — surface consequential decisions mid-turn.

A blocking tool: the handler awaits a host-injected callback that renders the
question and returns the user's answer, so the agent loop pauses in place and
resumes with the answer as an ordinary ``tool_result``. Hosts without a human
attached (evals, automated runs) simply never call :func:`build_ask_user` — the
schema then never reaches the model, which is the whole headless story.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from toolforge.registry import RegisteredTool, ToolContext, ToolResult

# ask_user calls in one batch must not race for the one human input channel;
# the registry's serial-group machinery runs them one at a time, in emission order.
USER_SERIAL_GROUP = "user"

_MIN_OPTIONS = 2
_MAX_OPTIONS = 4


@dataclass(frozen=True)
class AskOption:
    label: str
    description: str
    recommended: bool = False


@dataclass(frozen=True)
class AskUserRequest:
    """A validated question, handed to the host callback which owns all I/O."""

    question: str
    context: str
    options: tuple[AskOption, ...]


class AskUserUnavailableError(RuntimeError):
    """Raised by a callback when no user can actually be reached (e.g. stdin
    closed mid-session). The handler turns it into an ``is_error`` result so
    the model sees "asking failed" — never a fabricated answer."""


# The callback renders the request however the host likes (the REPL prints
# numbered options and reads stdin) and returns the user's answer: either an
# option's label verbatim or free-form text. If the user cannot be reached it
# raises AskUserUnavailableError instead of synthesizing an answer.
AskUserCallback = Callable[[AskUserRequest], Awaitable[str]]

_DESCRIPTION = """\
Ask the user to make a decision you should not make silently. Presents a question with \
the constraints behind it and 2-4 concrete options; the user picks one or answers in \
their own words, and their answer comes back as this tool's result while the task \
continues in place.

Use it when a decision would be baked into a forged tool's spec or tests, when an \
action is hard to reverse or visible outside the sandbox (spending money, choosing a \
cloud API, writing large artifacts to the workspace), or when the user's intent \
genuinely branches in ways that change the outcome. Asking is also fine whenever you \
are unsure what the user wants — a short question beats a wrong assumption.

Do not use it for anything a tool call, the registry, or the docs can answer, or for \
trivial reversible choices you can simply make. Batch related decisions into a single \
question rather than asking several times in a row. In `context`, state the constraints \
and trade-offs before the options so the options follow from them; mark at most one \
option `recommended` and use its description to say why. The user may ignore your \
options entirely and answer free-form — treat that answer as authoritative."""

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The single decision to be made, phrased as a direct question.",
        },
        "context": {
            "type": "string",
            "description": (
                "Why this decision matters: the constraints, trade-offs, and "
                "consequences of each direction. Written before the options so "
                "they follow from it."
            ),
        },
        "options": {
            "type": "array",
            "minItems": _MIN_OPTIONS,
            "maxItems": _MAX_OPTIONS,
            "description": "2-4 concrete, mutually exclusive ways to decide.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short name for the option (1-5 words).",
                    },
                    "description": {
                        "type": "string",
                        "description": "What choosing this means and its trade-offs.",
                    },
                    "recommended": {
                        "type": "boolean",
                        "description": (
                            "Mark at most one option true: your pick, with the "
                            "rationale in its description."
                        ),
                    },
                },
                "required": ["label", "description"],
            },
        },
    },
    "required": ["question", "context", "options"],
}


def _error(message: str) -> ToolResult:
    return ToolResult(tool_use_id="", content=message, is_error=True)


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def _parse_options(raw: Any) -> tuple[AskOption, ...] | str:
    """Return validated options, or a human-actionable problem description."""
    if not isinstance(raw, list) or not (_MIN_OPTIONS <= len(raw) <= _MAX_OPTIONS):
        return f"'options' must be a list of {_MIN_OPTIONS}-{_MAX_OPTIONS} options"
    options: list[AskOption] = []
    for i, item in enumerate(raw, start=1):
        if (
            not isinstance(item, dict)
            or _blank(item.get("label"))
            or _blank(item.get("description"))
        ):
            return (
                f"option {i} must be an object with non-empty string "
                "'label' and 'description' fields"
            )
        recommended = item.get("recommended", False)
        if not isinstance(recommended, bool):
            return f"option {i}: 'recommended' must be a boolean"
        options.append(
            AskOption(
                label=item["label"].strip(),
                description=item["description"].strip(),
                recommended=recommended,
            )
        )
    if sum(o.recommended for o in options) > 1:
        return "mark at most one option 'recommended'"
    return tuple(options)


def build_ask_user(ask: AskUserCallback) -> RegisteredTool:
    """Build ``ask_user`` bound to the host's *ask* callback.

    TRUSTED: the result is the user's own words — the user is the principal,
    so no safety envelope applies.
    """

    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if _blank(inp.get("question")):
            return _error("[ask_user error: 'question' must be a non-empty string]")
        if _blank(inp.get("context")):
            return _error(
                "[ask_user error: 'context' must be a non-empty string stating the "
                "constraints and trade-offs behind the question]"
            )
        options = _parse_options(inp.get("options"))
        if isinstance(options, str):
            return _error(f"[ask_user error: {options}]")

        request = AskUserRequest(
            question=inp["question"].strip(),
            context=inp["context"].strip(),
            options=options,
        )
        try:
            answer = (await ask(request)).strip()
        except AskUserUnavailableError as exc:
            return _error(
                f"[ask_user error: could not reach the user ({exc}) — no answer was "
                "given; decide with your best judgment and say so in your final reply]"
            )
        labels = {o.label for o in request.options}
        if answer in labels:
            content = f'User chose: "{answer}"'
        else:
            content = f"User answered: {answer}"
        return ToolResult(tool_use_id="", content=content)

    return RegisteredTool(
        name="ask_user",
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=handler,
        trust="TRUSTED",
        serial_group=USER_SERIAL_GROUP,
    )
