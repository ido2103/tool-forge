"""Turning a manifest into a live, sandbox-executing ``RegisteredTool``.

``install_runner`` ships the harness-owned runner into the tools dir (so it is
read-only inside the container alongside the tools it runs), and
``build_forged_tool`` builds the ``RegisteredTool`` whose handler executes
``python3 /tools/_runner.py <name> <b64-json>`` in the shared sandbox
container. Used by both promotion (mid-session) and the boot loader — a tool
behaves identically whether it was forged a minute or a month ago.
"""

from __future__ import annotations

import base64
import importlib.resources
import json
from pathlib import Path
from typing import Any

from toolforge.forge.manifest import Manifest
from toolforge.registry import RegisteredTool, ToolContext, ToolResult
from toolforge.sandbox import SANDBOX_SERIAL_GROUP, BashSandbox

RUNNER_FILENAME = "_runner.py"


def install_runner(tools_path: Path) -> None:
    """Copy the runner into ``<tools_path>/_runner.py``. Idempotent.

    Rewritten on every boot so the deployed runner never drifts from the
    packaged source. ``_runner.py`` is not a valid tool name (fails NAME_RE),
    so it can never collide with a tool directory.
    """
    source = importlib.resources.files("toolforge.forge").joinpath("runner.py").read_text("utf-8")
    tools_path.mkdir(parents=True, exist_ok=True)
    (tools_path / RUNNER_FILENAME).write_text(source, encoding="utf-8")


def build_forged_tool(manifest: Manifest, sandbox: BashSandbox) -> RegisteredTool:
    """Build the live ``RegisteredTool`` for a promoted forged tool.

    Always UNVERIFIED (model-written code — its output is never trusted into
    context unwrapped, network or not) and in the sandbox serial group (it
    shares the container and /workspace with run_bash). Timeout is the global
    ``command_timeout``; per-tool timeouts are a manifest schema_version bump
    away if ever needed.
    """
    name = manifest.name

    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        payload = base64.b64encode(json.dumps(inp).encode("utf-8")).decode("ascii")
        # name is NAME_RE-validated at promotion and load time and the payload
        # is base64 (shell-inert charset), so this composition is injection-safe.
        result = await sandbox.run(f"python3 /tools/{RUNNER_FILENAME} {name} {payload}")
        if result.timed_out:
            return ToolResult(tool_use_id="", content=result.stdout, is_error=True)
        # The runner's stdout IS the tool's result (or its bracketed error
        # message) — no [exit code: N] suffix; forged tools return a value,
        # not a shell transcript.
        content = result.stdout.rstrip("\n")
        if result.exit_code == 0:
            return ToolResult(tool_use_id="", content=content, is_error=False)
        if not content:
            content = (
                f"[forged-tool harness error: {name!r} produced no output "
                f"(exit code {result.exit_code})]"
            )
        return ToolResult(tool_use_id="", content=content, is_error=True)

    return RegisteredTool(
        name=name,
        description=manifest.description,
        input_schema=manifest.input_schema,
        handler=handler,
        trust="UNVERIFIED",
        serial_group=SANDBOX_SERIAL_GROUP,
    )
