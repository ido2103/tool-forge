"""``ask_user`` — validation, answer semantics, and loop integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.orchestrator._harness import FakeProviderClient, assistant_text, assistant_tool_use

from toolforge.orchestrator.ask_user import (
    USER_SERIAL_GROUP,
    AskUserRequest,
    AskUserUnavailableError,
    build_ask_user,
)
from toolforge.orchestrator.loop import Orchestrator
from toolforge.providers import Message
from toolforge.registry import ToolContext, ToolRegistry, ToolResult

MakeOrchestrator = Callable[[list[Message | Exception]], tuple[Orchestrator, FakeProviderClient]]


def _valid_input(**overrides: Any) -> dict[str, Any]:
    inp: dict[str, Any] = {
        "question": "Local Whisper or cloud STT?",
        "context": "Local is free but slow; cloud costs money per minute.",
        "options": [
            {"label": "Local Whisper", "description": "Free, slower.", "recommended": True},
            {"label": "Cloud STT", "description": "Fast, costs money."},
        ],
    }
    inp.update(overrides)
    return inp


class ScriptedAsk:
    """Callback stand-in: returns queued answers, records every request."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[AskUserRequest] = []

    async def __call__(self, request: AskUserRequest) -> str:
        self.requests.append(request)
        return self.answers.pop(0)


async def _call(inp: dict[str, Any], ask: ScriptedAsk) -> ToolResult:
    tool = build_ask_user(ask)
    return await tool.handler(inp, ToolContext())


# ── handler semantics ────────────────────────────────────────────────────────


async def test_option_pick_returns_label_verbatim() -> None:
    ask = ScriptedAsk("Local Whisper")
    result = await _call(_valid_input(), ask)
    assert not result.is_error
    assert result.content == 'User chose: "Local Whisper"'
    (request,) = ask.requests
    assert request.question == "Local Whisper or cloud STT?"
    assert [o.label for o in request.options] == ["Local Whisper", "Cloud STT"]
    assert request.options[0].recommended and not request.options[1].recommended


async def test_free_form_answer_passes_through() -> None:
    ask = ScriptedAsk("neither — use the paid API I already have keys for")
    result = await _call(_valid_input(), ask)
    assert not result.is_error
    assert result.content == "User answered: neither — use the paid API I already have keys for"


def test_registered_shape() -> None:
    tool = build_ask_user(ScriptedAsk())
    assert tool.name == "ask_user"
    assert tool.trust == "TRUSTED"
    assert tool.serial_group == USER_SERIAL_GROUP
    assert tool.input_schema["required"] == ["question", "context", "options"]
    # Generation order is reasoning order: context must precede options.
    keys = list(tool.input_schema["properties"])
    assert keys.index("context") < keys.index("options")


async def test_unreachable_user_is_error_never_an_answer() -> None:
    async def no_user(request: AskUserRequest) -> str:
        raise AskUserUnavailableError("stdin closed — no interactive user attached")

    tool = build_ask_user(no_user)
    result = await tool.handler(_valid_input(), ToolContext())
    assert result.is_error
    assert isinstance(result.content, str)
    assert "could not reach the user" in result.content
    assert "stdin closed" in result.content
    # A failure must never masquerade as something the user said.
    assert "User answered" not in result.content


# ── validation: actionable errors, callback never invoked ────────────────────


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"question": "  "}, "'question'"),
        ({"context": ""}, "'context'"),
        ({"options": []}, "2-4 options"),
        (
            {"options": [{"label": "only", "description": "one"}]},
            "2-4 options",
        ),
        (
            {"options": [{"label": f"o{i}", "description": "d"} for i in range(5)]},
            "2-4 options",
        ),
        (
            {
                "options": [
                    {"label": "a", "description": "d", "recommended": True},
                    {"label": "b", "description": "d", "recommended": True},
                ]
            },
            "at most one option",
        ),
        (
            {"options": [{"label": "a", "description": "d"}, {"label": " "}]},
            "option 2",
        ),
    ],
)
async def test_invalid_input_is_actionable_error(overrides: dict[str, Any], fragment: str) -> None:
    ask = ScriptedAsk("should never be consumed")
    result = await _call(_valid_input(**overrides), ask)
    assert result.is_error
    assert isinstance(result.content, str)
    assert fragment in result.content
    assert ask.requests == []  # invalid input never reaches the user


# ── loop integration ─────────────────────────────────────────────────────────


ASK_INPUT = {
    "question": "Q?",
    "context": "Because.",
    "options": [
        {"label": "A", "description": "a"},
        {"label": "B", "description": "b"},
    ],
}


async def test_answer_flows_back_as_tool_result(
    make_orchestrator: MakeOrchestrator, registry: ToolRegistry
) -> None:
    registry.register(build_ask_user(ScriptedAsk("A")))
    orch, client = make_orchestrator(
        [
            assistant_tool_use(("t1", "ask_user", dict(ASK_INPUT))),
            assistant_text("done"),
        ]
    )
    reply = await orch.run("task", [], system_prompt="s")
    assert reply == "done"
    # The second send() carries the tool_result with the user's answer.
    result_msg = client.calls[1]["messages"][-1]
    assert 'User chose: "A"' in str(result_msg.content)


async def test_parallel_questions_serialize_in_emission_order(
    make_orchestrator: MakeOrchestrator, registry: ToolRegistry
) -> None:
    ask = ScriptedAsk("first answer", "second answer")
    registry.register(build_ask_user(ask))
    second = dict(ASK_INPUT, question="Second Q?")
    orch, _client = make_orchestrator(
        [
            assistant_tool_use(
                ("t1", "ask_user", dict(ASK_INPUT)),
                ("t2", "ask_user", second),
            ),
            assistant_text("done"),
        ]
    )
    assert await orch.run("task", [], system_prompt="s") == "done"
    assert [r.question for r in ask.requests] == ["Q?", "Second Q?"]
