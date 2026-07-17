"""Orchestrator-loop fixtures. Harness classes/factories live in ``_harness.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.orchestrator._harness import FakeProviderClient, make_tool

from toolforge.orchestrator.hooks import HookManager
from toolforge.orchestrator.loop import Orchestrator
from toolforge.providers import Message
from toolforge.registry import ToolContext, ToolRegistry, ToolResult


@pytest.fixture
def registry() -> ToolRegistry:
    """A registry with one ``echo`` tool that returns its ``text`` input."""
    reg = ToolRegistry(ToolContext())

    async def echo(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(tool_use_id="", content=str(inp.get("text", "ok")))

    reg.register(make_tool("echo", echo))
    return reg


@pytest.fixture
def hooks() -> HookManager:
    return HookManager()


@pytest.fixture
def make_orchestrator(
    registry: ToolRegistry, hooks: HookManager
) -> Callable[..., tuple[Orchestrator, FakeProviderClient]]:
    """Factory: given a response script, wire an Orchestrator + FakeProviderClient."""

    def build(
        script: list[Message | Exception], *, max_iterations: int = 5
    ) -> tuple[Orchestrator, FakeProviderClient]:
        client = FakeProviderClient(script)
        orch = Orchestrator(
            client=client,
            registry=registry,
            hooks=hooks,
            model="fake-model",
            max_tokens=1024,
            max_iterations=max_iterations,
        )
        orch._TRANSIENT_DELAY_S = 0.0  # no real sleeps in tests
        return orch, client

    return build
