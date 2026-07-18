"""The ``run_bash`` seed tool — the orchestrator's one hand-written primitive.

Trusted (hand-written) code, but the *commands* are model-chosen, so it executes
inside the Docker sandbox rather than on the host. Composable-primitive by
design (per the granularity principle): a general shell, not a task-specific
mega-tool.
"""

from __future__ import annotations

from typing import Any

from toolforge.registry import RegisteredTool, ToolContext, ToolResult, Trust
from toolforge.sandbox.bash import BashSandbox

_DESCRIPTION = """\
Run a shell command inside an isolated Docker container (python:3.12-slim) and \
return its combined stdout+stderr and exit code.

Use this to inspect files, run scripts, install packages, and generally do work \
in the sandbox. Do NOT use it for actions that must affect the host machine — it \
cannot; everything runs in the container.

Environment and constraints:
- The working directory is /workspace, a directory shared with the host. Write \
files you want to keep there. Nothing outside /workspace is guaranteed to persist.
- Each call runs in a FRESH shell: `cd` and environment variables set in one call \
do NOT carry over to the next. Use absolute paths (e.g. /workspace/build/run.py) \
and set any needed env vars inline in the same command.
- Python 3.12, pip, and standard build tools are available. `pip install <pkg>` \
works when the sandbox has network access (the default); if the network is \
disabled, installs and network calls will fail.
- The image is minimal: `curl`, `wget`, and `git` are NOT installed. For HTTP \
requests, use python3 with urllib.request, or `pip install httpx`.
- The exit code is reported automatically as `[exit code: N]` after the output. \
Do not append `echo $?` or similar exit markers: a trailing `; echo ...` makes \
the shell report echo's exit code (0) and hides the real failure. Commands run \
with pipefail, so a failure anywhere in a pipeline is reported.
- Output is capped; very large output is truncated head+tail. Filter with \
grep/head/tail to see what you need.
- Long commands are killed after a timeout (default from config; override with \
the `timeout` parameter, in seconds)."""

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The shell command to run, e.g. 'python3 /workspace/x.py'.",
        },
        "timeout": {
            "type": "integer",
            "description": "Optional per-command timeout in seconds; defaults to the sandbox config.",
        },
    },
    "required": ["command"],
}


_SIGPIPE_EXIT = 141

# All sandbox-backed tools share one container and one /workspace, so calls in a
# batch must run one at a time, in emission order (the model writes a file, then
# runs it). Forged tools that execute in the sandbox reuse this group.
SANDBOX_SERIAL_GROUP = "sandbox"


def build_run_bash(sandbox: BashSandbox) -> RegisteredTool:
    """Build the ``run_bash`` RegisteredTool bound to *sandbox*.

    Trust is derived from the sandbox's network posture rather than hardcoded.
    The *code* is hand-written and trusted, but the *output* is not: with the
    network up, ``curl``/``pip``/any fetch can pipe attacker-controlled text into
    stdout and thus into context. Per docs/registry.md, anything touching the
    outside world is UNVERIFIED, so results get the prompt-injection envelope.
    With ``network="none"`` the container cannot reach out, so output stays
    TRUSTED and avoids paying the warning's token cost on every call.
    """
    trust: Trust = "UNVERIFIED" if sandbox.network_enabled else "TRUSTED"

    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = inp.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(
                tool_use_id="",
                content="[run_bash error: 'command' must be a non-empty string]",
                is_error=True,
            )
        timeout = inp.get("timeout")
        if timeout is not None and not isinstance(timeout, int):
            return ToolResult(
                tool_use_id="",
                content="[run_bash error: 'timeout' must be an integer number of seconds]",
                is_error=True,
            )

        result = await sandbox.run(command, timeout=timeout)
        if result.timed_out:
            return ToolResult(tool_use_id="", content=result.stdout, is_error=True)
        body = result.stdout
        if body and not body.endswith("\n"):
            body += "\n"
        # 141 = 128+SIGPIPE. Under pipefail, a producer killed because an
        # early-exiting consumer closed the pipe (`seq 1e6 | head -1`) reports
        # 141 even though the pipeline delivered exactly what was asked — and
        # the description above steers the model toward `| head`/`| tail`.
        # Treat it as success; the annotation keeps 141 from reading as failure.
        if result.exit_code == _SIGPIPE_EXIT:
            content = (
                f"{body}[exit code: 141 (SIGPIPE: pipe consumer exited early; treated as success)]"
            )
            return ToolResult(tool_use_id="", content=content, is_error=False)
        content = f"{body}[exit code: {result.exit_code}]"
        return ToolResult(tool_use_id="", content=content, is_error=result.exit_code != 0)

    return RegisteredTool(
        name="run_bash",
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=handler,
        trust=trust,
        serial_group=SANDBOX_SERIAL_GROUP,
    )
