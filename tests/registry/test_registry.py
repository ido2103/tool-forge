"""Registry tests — registration lifecycle, schema shape, execution, wrapping."""

from __future__ import annotations

from typing import Any

import pytest

from toolforge.registry import (
    RegisteredTool,
    ToolContext,
    ToolRegistry,
    ToolResult,
)


def _echo_tool(name: str = "echo", *, trust: str = "TRUSTED") -> RegisteredTool:
    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(tool_use_id="", content=str(inp.get("text", "")))

    return RegisteredTool(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=handler,
        trust=trust,  # type: ignore[arg-type]
    )


def test_register_and_get_schemas_anthropic_shape() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool())
    schemas = reg.get_schemas()
    assert schemas == [
        {
            "name": "echo",
            "description": "echo description",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }
    ]


def test_register_duplicate_raises() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_echo_tool())


def test_register_replace_flag() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool())
    replacement = _echo_tool()
    replacement.description = "new description"
    reg.register(replacement, replace=True)
    assert reg.get_schemas()[0]["description"] == "new description"


def test_replace_registers_when_absent() -> None:
    reg = ToolRegistry()
    reg.replace(_echo_tool())
    assert reg.has("echo")


async def test_replace_swaps_handler() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool())

    async def new_handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(tool_use_id="", content="REPLACED")

    swapped = _echo_tool()
    swapped.handler = new_handler
    reg.replace(swapped)
    result = await reg.execute("echo", {"text": "ignored"})
    assert isinstance(result.content, str)
    assert "REPLACED" in result.content  # wrapped in the safety envelope


def test_unregister_removes() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool())
    reg.unregister("echo")
    assert not reg.has("echo")
    assert reg.get_schemas() == []


async def test_execute_sets_tool_use_id() -> None:
    # execute() leaves tool_use_id as the handler set it; the loop overwrites it
    # with the real call id. Here we assert execute returns the handler's result.
    reg = ToolRegistry()
    reg.register(_echo_tool())
    result = await reg.execute("echo", {"text": "hi"})
    assert isinstance(result, ToolResult)


async def test_execute_unknown_raises_keyerror() -> None:
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        await reg.execute("nonexistent", {})


async def test_execute_wraps_trusted_string() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool(trust="TRUSTED"))
    result = await reg.execute("echo", {"text": "payload"})
    assert isinstance(result.content, str)
    assert '<tool_result tool="echo" trust="TRUSTED">' in result.content
    assert "payload" in result.content
    assert "prompt_injection_warning" not in result.content


async def test_execute_wraps_unverified_with_warning() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool(trust="UNVERIFIED"))
    result = await reg.execute("echo", {"text": "payload"})
    assert isinstance(result.content, str)
    assert 'trust="UNVERIFIED"' in result.content
    assert "prompt_injection_warning" in result.content
    assert "<external_content>" in result.content


async def test_execute_list_content_passes_through() -> None:
    reg = ToolRegistry()

    async def multimodal(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(tool_use_id="", content=[{"type": "text", "text": "x"}])

    tool = _echo_tool()
    tool.handler = multimodal
    reg.register(tool)
    result = await reg.execute("echo", {})
    assert result.content == [{"type": "text", "text": "x"}]  # unwrapped


def test_get_schemas_fresh_list_reflects_live_add() -> None:
    reg = ToolRegistry()
    reg.register(_echo_tool("first"))
    first = reg.get_schemas()
    assert len(first) == 1

    # Mid-task: forge registers a second tool. Next get_schemas() call sees it,
    # and the previously returned list is not retroactively mutated.
    reg.register(_echo_tool("second"))
    assert len(first) == 1  # prior snapshot untouched
    second = reg.get_schemas()
    assert {s["name"] for s in second} == {"first", "second"}
