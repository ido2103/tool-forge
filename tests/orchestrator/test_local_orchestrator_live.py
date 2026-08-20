"""Live smoke — the orchestrator loop on a local OpenAI-compatible server.

Run manually:  uv run pytest -m live tests/orchestrator/test_local_orchestrator_live.py
Requires TOOLFORGE_ORCHESTRATOR_BACKEND=local and a running server (llama.cpp /
vLLM / LM Studio) per TOOLFORGE_ORCHESTRATOR_HOST/PORT/MODEL. Missing pieces
skip, not fail. No Docker or Anthropic credentials needed — the tools are
in-memory fakes; what's under test is the real loop over the real local model
(tool-id round-trip, finish_reason normalization, streaming).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from toolforge.config import OrchestratorSettings
from toolforge.orchestrator.hooks import HookManager
from toolforge.orchestrator.loop import Orchestrator
from toolforge.providers import Message, OpenAICompatClient, ToolUseBlock
from toolforge.registry import ToolContext, ToolRegistry, ToolResult

from ._harness import make_tool

pytestmark = pytest.mark.live


def _local_orch_settings_or_skip() -> OrchestratorSettings:
    settings = OrchestratorSettings()
    if settings.backend != "local":
        pytest.skip("TOOLFORGE_ORCHESTRATOR_BACKEND != local")
    try:
        httpx.get(f"{settings.base_url}/models", timeout=2.0).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"no local server reachable at {settings.base_url}")
    return settings


def _orchestrator(settings: OrchestratorSettings, registry: ToolRegistry) -> Orchestrator:
    return Orchestrator(
        client=OpenAICompatClient(settings),
        registry=registry,
        hooks=HookManager(),
        model=settings.model,
        max_tokens=min(settings.max_tokens_per_turn, 2048),
        max_iterations=5,
    )


async def test_local_orchestrator_plain_turn() -> None:
    settings = _local_orch_settings_or_skip()
    orch = _orchestrator(settings, ToolRegistry(ToolContext()))

    history: list[Message] = []
    final = await orch.run(
        "Reply with exactly one word: pong",
        history,
        system_prompt="You are a connectivity probe. Follow instructions exactly.",
    )

    assert final.strip()
    assert history[-1].stop_reason == "end_turn"


async def test_local_orchestrator_tool_round_trip() -> None:
    settings = _local_orch_settings_or_skip()
    registry = ToolRegistry(ToolContext())

    async def echo(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(tool_use_id="", content=str(inp.get("text", "pong")))

    registry.register(make_tool("echo", echo))
    orch = _orchestrator(settings, registry)

    history: list[Message] = []
    final = await orch.run(
        "Call the echo tool once, then tell me what it returned.",
        history,
        system_prompt="You have an echo tool. Use it when asked, then answer in text.",
    )

    tool_uses = [b for m in history for b in m.content if isinstance(b, ToolUseBlock)]
    assert tool_uses, "the local model never called the echo tool"
    assert tool_uses[0].name == "echo"
    assert final.strip()
