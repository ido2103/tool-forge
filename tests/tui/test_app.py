"""Pilot tests for the Textual app — boot, streaming turns, slash commands.

Everything runs on stubs (`tests/tui/_harness.py`); the real sandbox/model path
is manual (`uv run toolforge-tui`).
"""

from __future__ import annotations

from tests.orchestrator._harness import assistant_text, assistant_tool_use

from toolforge.config import SandboxSettings
from toolforge.providers import PermanentProviderError
from toolforge.tui.app import ToolforgeApp
from toolforge.tui.widgets import ChatLog

from tests.tui._harness import chat_texts, make_stub_host


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
