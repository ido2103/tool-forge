"""Pilot tests for the Textual app — boot, streaming turns, slash commands.

Everything runs on stubs (`tests/tui/_harness.py`); the real sandbox/model path
is manual (`uv run toolforge-tui`).
"""

from __future__ import annotations

from typing import Any

from tests.orchestrator._harness import assistant_text, assistant_tool_use

from toolforge.config import SandboxSettings
from toolforge.orchestrator.ask_user import build_ask_user
from toolforge.orchestrator.hooks import HookEvent
from toolforge.providers import PermanentProviderError
from toolforge.tui.app import ToolforgeApp
from toolforge.tui.screens import AskUserScreen
from textual.widgets import Input, Static

from toolforge.tui.widgets import ChatLog

from tests.tui._harness import chat_texts, make_stub_host, tool_result_text


async def test_boot_enables_input_and_shows_findings(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(
        sandbox_settings,
        [],
        loaded_tools=["fetch_rss"],
        tool_store_warnings=["bad_dir: no manifest"],
    )
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.prompt.disabled
        assert "ready" in app.sub_title
        texts = chat_texts(app, ".msg")
        assert any("fetch_rss" in t for t in texts)
        assert any("bad_dir" in t for t in texts)


async def test_boot_failure_keeps_input_disabled(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(sandbox_settings, [], sandbox_script=[(1, b"docker daemon down")])
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.prompt.disabled
        assert "FAILED" in app.sub_title
        errors = chat_texts(app, ".error")
        assert any("Sandbox startup failed" in t for t in errors)


async def test_turn_streams_and_appends_history(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(sandbox_settings, [assistant_text("hello from the model")])
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("do a thing")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.chat.answer_text == "hello from the model"
        # user turn + assistant reply, mutated in place by the loop
        assert [m.role for m in app._history] == ["user", "assistant"]
        assert not app.turn_running
        assert not app.prompt.disabled


async def test_turn_with_tool_call_completes(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(
        sandbox_settings,
        [assistant_tool_use(("tu_1", "echo", {"text": "x"})), assistant_text("done")],
    )
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("use the tool")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.chat.answer_text == "done"
        # user, assistant tool_use, tool_result, assistant text
        assert len(app._history) == 4


async def test_provider_error_is_reported_not_fatal(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(sandbox_settings, [PermanentProviderError("bad credentials")])
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("boom")
        await app.workers.wait_for_complete()
        await pilot.pause()
        errors = chat_texts(app, ".error")
        assert any("bad credentials" in t for t in errors)
        assert not app.prompt.disabled  # the app survives to take another turn


async def test_slash_new_clears_history(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(sandbox_settings, [assistant_text("hi")])
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("hello")
        await app.workers.wait_for_complete()
        assert app._history
        await app.handle_submit("/new")
        await pilot.pause()
        assert app._history == []
        notes = chat_texts(app, ".system")
        assert any("history cleared" in t for t in notes)


async def test_unknown_command_gets_a_hint(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(sandbox_settings, [])
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("/bogus")
        await pilot.pause()
        notes = chat_texts(app, ".system")
        assert any("/bogus" in t for t in notes)


async def test_reset_recycles_container_and_state(sandbox_settings: SandboxSettings) -> None:
    # boot start + post-teardown restart
    host = make_stub_host(
        sandbox_settings, [assistant_text("hi")], sandbox_script=[(0, b"started"), (0, b"started")]
    )
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("hello")
        await app.workers.wait_for_complete()
        await app.handle_submit("/reset")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._history == []
        assert not app.prompt.disabled
        notes = chat_texts(app, ".system")
        assert any("container recycled" in t for t in notes)


async def test_chatlog_is_composed(sandbox_settings: SandboxSettings) -> None:
    app = ToolforgeApp(make_stub_host(sandbox_settings, []))
    async with app.run_test():
        assert isinstance(app.query_one("#chat"), ChatLog)


# ── tool activity + forge panel ──────────────────────────────────────────────


async def test_tool_activity_shows_orchestrator_calls(
    sandbox_settings: SandboxSettings,
) -> None:
    host = make_stub_host(
        sandbox_settings,
        [assistant_tool_use(("tu_1", "echo", {"text": "ping"})), assistant_text("done")],
    )
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("use the tool")
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = [str(w.content) for w in app.activity.query(".tool-row").results(Static)]
        assert len(rows) == 1
        assert "echo" in rows[0]
        assert "✓" in rows[0]


async def test_forge_panel_reveals_narrates_and_finishes(
    sandbox_settings: SandboxSettings,
) -> None:
    host = make_stub_host(sandbox_settings, [])
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        assert app.forge_panel.has_class("hidden")

        fire = host.hooks.fire
        await fire(
            HookEvent.ON_TOOL_PRE_EXECUTE,
            tool_name="forge_tool",
            call_id="f1",
            input={"name": "fetch_rss"},
            component="orchestrator",
        )
        await fire(HookEvent.ON_FORGE_PHASE, tool="fetch_rss", phase="authoring_tests")
        await fire(HookEvent.ON_FORGE_PHASE, tool="fetch_rss", phase="tests_ready", test_count=7)
        await fire(
            HookEvent.ON_FORGE_PHASE,
            tool="fetch_rss",
            phase="attempt",
            attempt=1,
            max_attempts=4,
        )
        await fire(
            HookEvent.ON_TOOL_PRE_EXECUTE,
            tool_name="run_tests",
            call_id="w1",
            input={},
            component="forge_worker",
        )
        await pilot.pause()

        panel = app.forge_panel
        assert not panel.has_class("hidden")
        assert "attempt 1/4" in panel.status_text
        feed = [str(w.content) for w in panel.query(".feed-row").results(Static)]
        assert any("run_tests" in t for t in feed)
        # the worker's inner calls never pollute the orchestrator activity list
        rows = [str(w.content) for w in app.activity.query(".tool-row").results(Static)]
        assert not any("run_tests" in t for t in rows)

        await fire(HookEvent.ON_FORGE_PHASE, tool="fetch_rss", phase="candidate_ready", attempts=2)
        await fire(
            HookEvent.ON_TOOL_POST_EXECUTE,
            tool_name="forge_tool",
            call_id="f1",
            is_error=False,
            latency_ms=120000,
            component="orchestrator",
        )
        await pilot.pause()
        assert "candidate ready" in panel.status_text
        rows = [str(w.content) for w in app.activity.query(".tool-row").results(Static)]
        assert any("forge_tool" in t and "✓" in t for t in rows)


async def test_forge_failure_marks_panel(sandbox_settings: SandboxSettings) -> None:
    host = make_stub_host(sandbox_settings, [])
    app = ToolforgeApp(host)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        fire = host.hooks.fire
        await fire(
            HookEvent.ON_TOOL_PRE_EXECUTE,
            tool_name="forge_tool",
            call_id="f2",
            input={},
            component="orchestrator",
        )
        await fire(HookEvent.ON_FORGE_PHASE, tool="slugify", phase="failed")
        await fire(
            HookEvent.ON_TOOL_POST_EXECUTE,
            tool_name="forge_tool",
            call_id="f2",
            is_error=True,
            latency_ms=1,
            component="orchestrator",
        )
        await pilot.pause()
        assert "✗" in app.forge_panel.status_text


# ── ask_user modal ───────────────────────────────────────────────────────────


ASK_INPUT = {
    "question": "Which STT backend?",
    "context": "Local is free but slow; cloud is fast but paid.",
    "options": [
        {"label": "Local Whisper", "description": "free, slower", "recommended": True},
        {"label": "Cloud API", "description": "fast, costs money"},
    ],
}


def _ask_host(sandbox_settings: SandboxSettings) -> tuple[ToolforgeApp, list[str]]:
    """App whose stub host has ask_user wired to the modal, plus its script."""
    host = make_stub_host(
        sandbox_settings,
        [assistant_tool_use(("tu_1", "ask_user", dict(ASK_INPUT))), assistant_text("done")],
    )
    app = ToolforgeApp(host)
    host.registry.register(build_ask_user(app.ask_user))
    return app, []


async def _wait_for_modal(app: ToolforgeApp, pilot: Any) -> AskUserScreen:
    for _ in range(100):
        await pilot.pause()
        if isinstance(app.screen, AskUserScreen):
            return app.screen
    raise AssertionError("ask_user modal never appeared")


async def test_ask_user_button_returns_label_verbatim(
    sandbox_settings: SandboxSettings,
) -> None:
    app, _ = _ask_host(sandbox_settings)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("transcribe my audio")
        await _wait_for_modal(app, pilot)
        await pilot.click("#opt-0")
        await app.workers.wait_for_complete()
        await pilot.pause()
        tool_results = tool_result_text(app._history[2])
        assert 'User chose: "Local Whisper"' in tool_results
        assert app.chat.answer_text == "done"


async def test_ask_user_free_text_answer(sandbox_settings: SandboxSettings) -> None:
    app, _ = _ask_host(sandbox_settings)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("transcribe my audio")
        screen = await _wait_for_modal(app, pilot)
        free = screen.query_one("#ask-free", Input)
        free.focus()
        await pilot.pause()
        await pilot.press(*"use deepgram", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        tool_results = tool_result_text(app._history[2])
        assert "User answered: use deepgram" in tool_results


async def test_ask_user_modal_free_text_never_reaches_prompt(
    sandbox_settings: SandboxSettings,
) -> None:
    app, _ = _ask_host(sandbox_settings)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("transcribe my audio")
        screen = await _wait_for_modal(app, pilot)
        screen.query_one("#ask-free", Input).focus()
        await pilot.pause()
        await pilot.press(*"hello", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # the modal's answer must not have been dispatched as a new task
        user_msgs = [m for m in app._history if m.role == "user" and not tool_result_text(m)]
        assert len(user_msgs) == 1


async def test_stop_during_question_dismisses_modal(
    sandbox_settings: SandboxSettings,
) -> None:
    app, _ = _ask_host(sandbox_settings)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await app.handle_submit("transcribe my audio")
        await _wait_for_modal(app, pilot)
        app.action_stop_turn()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not isinstance(app.screen, AskUserScreen)
        assert not app.turn_running
        # the aborted question surfaces as an [ABORTED] tool result, never an answer
        tool_results = tool_result_text(app._history[2])
        assert "ABORTED" in tool_results
