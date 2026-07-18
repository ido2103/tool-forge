"""Live forge test — the full promotion story against a real Docker container.

    uv run pytest -m live tests/forge/test_forge_live.py

Covers the whole slice end-to-end: candidate files written in the sandbox →
register_tool promotes → the forged tool executes in the container → a second
"session" reloads it from disk (reboot survival) → /tools is read-only to the
agent. Skips automatically if the docker daemon is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from toolforge.config import SandboxSettings
from toolforge.forge import (
    Candidate,
    CandidateStore,
    build_register_tool,
    install_runner,
    load_persisted_tools,
)
from toolforge.registry import ToolContext, ToolRegistry
from toolforge.sandbox.bash import BashSandbox

from tests._docker import DOCKER_SKIP_REASON, docker_available

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not docker_available(), reason=DOCKER_SKIP_REASON),
]

_TOOL_SOURCE = """\
def run(a, b):
    return {"sum": a + b}
"""


def _settings(tmp_path: Path) -> SandboxSettings:
    return SandboxSettings(
        _env_file=None,
        image="python:3.12-slim",
        network="none",
        workspace_path=tmp_path / "workspace",
        tools_path=tmp_path / "tools",
        command_timeout=30,
        output_cap=100_000,
    )


async def test_promote_call_reload_and_readonly(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    install_runner(settings.tools_path)
    sandbox = BashSandbox(settings)
    try:
        # ── Session 1: forge-shaped candidate → promote → call ──────────────
        # The candidate's code is written INTO the sandbox via run_bash-style
        # commands, exactly as the future build loop will leave it.
        write = await sandbox.run(
            "mkdir -p /workspace/build/add_numbers && "
            "cat > /workspace/build/add_numbers/tool.py <<'EOF'\n" + _TOOL_SOURCE + "EOF"
        )
        assert write.exit_code == 0

        store = CandidateStore()
        registry = ToolRegistry(ToolContext())
        register = build_register_tool(store, registry, sandbox, settings)
        store.put(
            Candidate(
                name="add_numbers",
                description="Add two numbers and return their sum as JSON.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "number", "description": "First addend."},
                        "b": {"type": "number", "description": "Second addend."},
                    },
                    "required": ["a", "b"],
                },
                behavior="Returns {'sum': a+b}.",
                gap_analysis="live test",
                code_path="/workspace/build/add_numbers/tool.py",
            )
        )
        promoted = await register.handler(
            {"holdout_evidence": "ran (1,2) and (-1,1) by hand", "name": "add_numbers"},
            ToolContext(),
        )
        assert not promoted.is_error, promoted.content

        result = await registry.execute("add_numbers", {"a": 2, "b": 40})
        assert isinstance(result.content, str)
        assert '{"sum": 42}' in result.content
        assert "prompt_injection_warning" in result.content  # UNVERIFIED envelope

        # Error path: a bad input surfaces the tool's TypeError, not silence.
        bad = await registry.execute("add_numbers", {"a": 2})
        assert bad.is_error
        assert "TypeError" in str(bad.content)

        # ── Read-only store: the agent cannot touch /tools ──────────────────
        ro = await sandbox.run("touch /tools/hack.txt")
        assert ro.exit_code != 0
        ro2 = await sandbox.run("rm -rf /tools/add_numbers")
        assert ro2.exit_code != 0
        assert (settings.tools_path / "add_numbers" / "tool.py").exists()

        # ── Session 2: fresh registry, same disk — reboot survival ──────────
        registry2 = ToolRegistry(ToolContext())
        loaded, warnings = load_persisted_tools(settings.tools_path, sandbox, registry2)
        assert loaded == ["add_numbers"]
        assert warnings == []
        result2 = await registry2.execute("add_numbers", {"a": 5, "b": 5})
        assert '{"sum": 10}' in str(result2.content)
    finally:
        sandbox.teardown()
