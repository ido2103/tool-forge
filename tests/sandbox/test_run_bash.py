"""run_bash tool tests — schema, input validation, exit-code → is_error mapping."""

from __future__ import annotations

from toolforge.config import SandboxSettings
from toolforge.registry import RegisteredTool, ToolContext
from toolforge.sandbox.bash import BashSandbox
from toolforge.sandbox.run_bash import build_run_bash

from tests.sandbox.test_bash import FakeRunner


def _tool_with(
    results: list[tuple[int | None, bytes] | BaseException], s: SandboxSettings
) -> RegisteredTool:
    sandbox = BashSandbox(s, runner=FakeRunner(results))
    return build_run_bash(sandbox)


def test_schema_requires_command(sandbox_settings: SandboxSettings) -> None:
    tool = _tool_with([], sandbox_settings)
    assert tool.name == "run_bash"
    assert tool.trust == "TRUSTED"
    assert tool.input_schema["required"] == ["command"]
    assert "command" in tool.input_schema["properties"]


async def test_missing_command_is_error(sandbox_settings: SandboxSettings) -> None:
    tool = _tool_with([], sandbox_settings)
    result = await tool.handler({}, ToolContext())
    assert result.is_error
    assert "command" in str(result.content)


async def test_non_int_timeout_is_error(sandbox_settings: SandboxSettings) -> None:
    tool = _tool_with([], sandbox_settings)
    result = await tool.handler({"command": "echo hi", "timeout": "soon"}, ToolContext())
    assert result.is_error
    assert "timeout" in str(result.content)


async def test_success_reports_exit_code_zero(sandbox_settings: SandboxSettings) -> None:
    tool = _tool_with([(0, b"started"), (0, b"hello\n")], sandbox_settings)
    result = await tool.handler({"command": "echo hello"}, ToolContext())
    assert not result.is_error
    assert "hello" in str(result.content)
    assert "[exit code: 0]" in str(result.content)


async def test_nonzero_exit_is_error(sandbox_settings: SandboxSettings) -> None:
    tool = _tool_with([(0, b"started"), (2, b"boom\n")], sandbox_settings)
    result = await tool.handler({"command": "false"}, ToolContext())
    assert result.is_error
    assert "[exit code: 2]" in str(result.content)


async def test_timeout_is_error(sandbox_settings: SandboxSettings) -> None:
    tool = _tool_with([(0, b"started"), TimeoutError()], sandbox_settings)
    result = await tool.handler({"command": "sleep 999", "timeout": 3}, ToolContext())
    assert result.is_error
    assert "timed out" in str(result.content)
