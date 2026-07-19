"""TUI-test harness: a stub Host on fakes — no Docker, no API, no credentials."""

from __future__ import annotations

from typing import Any

from textual.widgets import Static

from tests.orchestrator._harness import FakeProviderClient, make_tool
from tests.sandbox.test_bash import FakeRunner

from toolforge.config import SandboxSettings
from toolforge.forge import CandidateStore
from toolforge.orchestrator.bootstrap import Host
from toolforge.orchestrator.hooks import HookManager
from toolforge.orchestrator.loop import Orchestrator
from toolforge.providers import Message
from toolforge.registry import ToolContext, ToolRegistry, ToolResult
from toolforge.sandbox import BashSandbox

_CHUNK = 4


def chat_texts(app: Any, selector: str = ".msg") -> list[str]:
    """Plain text of every chat message matching *selector*."""
    return [str(w.content) for w in app.chat.query(selector).results(Static)]


class StreamingFakeClient(FakeProviderClient):
    """FakeProviderClient that also streams the scripted reply's text as deltas."""

    async def send(self, **kwargs: Any) -> Message:
        item = self._script[0] if self._script else None
        on_text = kwargs.get("on_text_delta")
        if isinstance(item, Message) and item.text and on_text is not None:
            for i in range(0, len(item.text), _CHUNK):
                await on_text(item.text[i : i + _CHUNK])
        return await super().send(**kwargs)


def make_stub_host(
    sandbox_settings: SandboxSettings,
    script: list[Message | Exception],
    *,
    sandbox_script: list[tuple[int | None, bytes] | BaseException] | None = None,
    loaded_tools: list[str] | None = None,
    tool_store_warnings: list[str] | None = None,
) -> Host:
    client = StreamingFakeClient(script)
    sandbox = BashSandbox(sandbox_settings, runner=FakeRunner(sandbox_script or [(0, b"started")]))
    hooks = HookManager()
    registry = ToolRegistry(ToolContext())

    async def echo(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(tool_use_id="", content=str(inp.get("text", "ok")))

    registry.register(make_tool("echo", echo))
    orchestrator = Orchestrator(
        client=client,
        registry=registry,
        hooks=hooks,
        model="fake-model",
        max_tokens=1024,
        max_iterations=5,
    )
    return Host(
        orchestrator=orchestrator,
        sandbox=sandbox,
        candidates=CandidateStore(),
        registry=registry,
        hooks=hooks,
        system_prompt="sys",
        model="fake-model",
        loaded_tools=loaded_tools or [],
        tool_store_warnings=tool_store_warnings or [],
    )
