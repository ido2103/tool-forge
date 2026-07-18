"""End-to-end spine test — real loop + registry + Docker sandbox + transcript.

The only fake is the provider (scripted responses); everything downstream is
real, so this proves the integration the REPL relies on: the loop dispatches a
run_bash call into a real container, the artifact lands on the host, results feed
back, and the whole exchange is written to the JSONL transcript.

Marked ``live`` (needs Docker); run with:  uv run pytest -m live
"""

from __future__ import annotations

from pathlib import Path

import pytest

from toolforge.config import SandboxSettings
from toolforge.orchestrator.hooks import HookManager
from toolforge.orchestrator.loop import Orchestrator
from toolforge.orchestrator.transcript import Transcript, new_run_path
from toolforge.providers import Message
from toolforge.registry import ToolContext, ToolRegistry
from toolforge.sandbox import BashSandbox, build_run_bash

from tests._docker import DOCKER_SKIP_REASON, docker_available
from tests.orchestrator._harness import FakeProviderClient, assistant_text, assistant_tool_use

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not docker_available(), reason=DOCKER_SKIP_REASON),
]


async def test_agent_runs_bash_in_real_container(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    settings = SandboxSettings(
        _env_file=None,
        image="python:3.12-slim",
        network="on",
        workspace_path=workspace,
        command_timeout=30,
        output_cap=100_000,
    )
    sandbox = BashSandbox(settings)
    registry = ToolRegistry(ToolContext())
    registry.register(build_run_bash(sandbox))

    transcript_path = new_run_path(tmp_path / "runs")
    orch = Orchestrator(
        client=FakeProviderClient(
            [
                assistant_tool_use(
                    (
                        "toolu_1",
                        "run_bash",
                        {
                            "command": "echo spine-ok > /workspace/smoke.txt && cat /workspace/smoke.txt"
                        },
                    )
                ),
                assistant_text("Wrote and read /workspace/smoke.txt."),
            ]
        ),
        registry=registry,
        hooks=HookManager(),
        model="fake",
        max_tokens=1024,
        max_iterations=5,
        transcript=Transcript(transcript_path),
    )

    history: list[Message] = []
    try:
        result = await orch.run("write and read a file", history, system_prompt="sys")
    finally:
        sandbox.teardown()

    assert result == "Wrote and read /workspace/smoke.txt."
    # The command actually ran in the container and wrote to the host mount.
    assert (workspace / "smoke.txt").read_text().strip() == "spine-ok"
    # The tool result fed back into history, carrying the command output.
    tool_result = history[2].content[0]
    assert getattr(tool_result, "content", "") and "spine-ok" in tool_result.content  # type: ignore[union-attr]
    # The whole exchange was transcribed.
    lines = transcript_path.read_text().splitlines()
    assert len(lines) == 4  # user, assistant(tool_use), user(tool_result), assistant(text)
