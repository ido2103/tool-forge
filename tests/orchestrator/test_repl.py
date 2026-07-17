"""REPL helper tests — argparse, styling, and the tool one-liner hooks.

The interactive loop and client wiring need credentials and a TTY, so only the
pure pieces are unit-tested here.
"""

from __future__ import annotations

import argparse

import pytest

import toolforge.orchestrator.repl as repl
from toolforge.orchestrator.hooks import HookEvent, HookManager


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toolforge")
    parser.add_argument("task", nargs="?")
    return parser


def test_argparser_optional_task() -> None:
    parser = _make_parser()
    assert parser.parse_args([]).task is None
    assert parser.parse_args(["do a thing"]).task == "do a thing"


def test_style_plain_when_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repl, "_USE_COLOR", False)
    assert repl._dim("hi") == "hi"
    assert repl._cyan("hi") == "hi"


def test_style_wraps_when_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repl, "_USE_COLOR", True)
    out = repl._dim("hi")
    assert out.startswith("\x1b[2m")
    assert out.endswith("\x1b[0m")
    assert "hi" in out


async def test_tool_oneliners_print(capsys: pytest.CaptureFixture[str]) -> None:
    hooks = HookManager()
    repl._install_tool_oneliners(hooks)
    await hooks.fire(
        HookEvent.ON_TOOL_PRE_EXECUTE,
        tool_name="run_bash",
        call_id="1",
        input={"command": "echo hi"},
        component="orchestrator",
    )
    await hooks.fire(
        HookEvent.ON_TOOL_POST_EXECUTE,
        tool_name="run_bash",
        call_id="1",
        is_error=False,
        latency_ms=42,
        component="orchestrator",
    )
    captured = capsys.readouterr().out
    assert "run_bash" in captured
    assert "echo hi" in captured
    assert "✓" in captured
    assert "42ms" in captured


async def test_tool_oneliner_error_mark(capsys: pytest.CaptureFixture[str]) -> None:
    hooks = HookManager()
    repl._install_tool_oneliners(hooks)
    await hooks.fire(
        HookEvent.ON_TOOL_POST_EXECUTE,
        tool_name="run_bash",
        call_id="1",
        is_error=True,
        latency_ms=10,
        component="orchestrator",
    )
    assert "✗" in capsys.readouterr().out
