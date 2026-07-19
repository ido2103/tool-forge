"""Worker-private tools — the forge worker's hands inside the build directory.

These are registered only in the :class:`~toolforge.forge.worker.ForgeWorker`'s
private registry, never in the orchestrator's. Two factories:

- ``write_tool_code`` writes the complete ``tool.py`` host-side (like the test
  author and promotion, the bind mount makes it visible in the container with
  no shell-quoting hazards). It is deliberately parameterless about paths: the
  worker cannot overwrite the test suite or scatter files through it, making
  that whole misuse class structurally impossible.
- ``run_tests`` runs the authored suite in the sandbox with the exact flags the
  harness verification uses. Its result is *advisory*: the harness reruns the
  suite from a pristine copy after the worker finishes, and only that run
  counts.

Both share ``SANDBOX_SERIAL_GROUP`` — the write is host-side but mutates the
directory the sandboxed pytest reads, so write→test sequences emitted in one
assistant turn must execute in emission order.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

from toolforge.registry import RegisteredTool, ToolContext, ToolResult, Trust
from toolforge.sandbox.bash import BashSandbox
from toolforge.sandbox.run_bash import SANDBOX_SERIAL_GROUP

TEST_RUN_TIMEOUT = 120


def pytest_command(name: str) -> str:
    """The suite-run command for ``build/<name>`` — shared verbatim by the
    worker's advisory ``run_tests`` and the harness's authoritative
    verification, so the worker never sees a flag mismatch between the two."""
    return (
        f"cd /workspace/build/{name} && "
        "python3 -m pytest -v --tb=short -p no:cacheprovider test_tool.py"
    )


_WRITE_DESCRIPTION_TEMPLATE = """\
Write the complete source of the tool implementation to \
/workspace/build/{name}/tool.py, replacing whatever is there. This is the only \
way to change the implementation — always pass the entire file, defining \
run(...) with keyword parameters exactly matching the spec's input_schema \
properties. The file is syntax-checked before writing; a syntax error or an \
import outside the Python standard library is rejected with the reason and \
nothing is written. Do not use this for anything except tool.py; use run_bash \
for scratch experiments. It does not run the tests — call run_tests after \
writing."""

_RUN_TESTS_DESCRIPTION = """\
Run the authored test suite against your current tool.py and return the pytest \
output. Use it after every write_tool_code call; read the failing assertions to \
decide the next fix. This run is advisory: when you finish, the harness reruns \
the suite from its own pristine copy of test_tool.py, and only that run counts \
— so editing the tests, adding conftest.py, or configuring pytest cannot make \
the build succeed. Do not rerun without changing tool.py first; identical code \
produces identical results and spends your budget."""


def _error(message: str) -> ToolResult:
    return ToolResult(tool_use_id="", content=message, is_error=True)


def _non_stdlib_imports(source: str) -> list[str]:
    """Import roots (anywhere in the module) outside the stdlib, sorted, deduped.

    Relative imports are reported as ``.`` — meaningless in a single-module
    tool and guaranteed to fail at runtime.
    """
    allowed = sys.stdlib_module_names | {"tool"}
    bad: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed:
                    bad.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                bad.add(".")
            elif node.module is not None:
                root = node.module.split(".")[0]
                if root not in allowed:
                    bad.add(root)
    return sorted(bad)


def build_write_tool_code(build_dir: Path, name: str) -> RegisteredTool:
    """Build ``write_tool_code`` bound to the host-side *build_dir* for *name*.

    TRUSTED: its output is harness-generated text (counts and error messages),
    never external content.
    """

    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = inp.get("code")
        if not isinstance(code, str) or not code.strip():
            return _error(
                "[write_tool_code error: 'code' must be the complete, non-empty source of tool.py]"
            )
        try:
            compile(code, "tool.py", "exec")
        except SyntaxError as exc:
            return _error(
                f"[write_tool_code error: tool.py has a syntax error at line "
                f"{exc.lineno}: {exc.msg}. Nothing was written — fix the code and "
                "send the complete file again.]"
            )
        bad = _non_stdlib_imports(code)
        if bad:
            names = ", ".join(repr(m) for m in bad)
            return _error(
                f"[write_tool_code error: tool.py imports {names}, which is not in "
                "the Python standard library. Forged tools run in a minimal "
                "container that is rebuilt between sessions, so third-party imports "
                "would break after a restart. Nothing was written — reimplement "
                "using the stdlib (e.g. urllib.request instead of requests, json "
                "instead of pydantic).]"
            )
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "tool.py").write_text(code, encoding="utf-8")
        n = code.count("\n") + 1
        return ToolResult(
            tool_use_id="",
            content=(
                f"[write_tool_code: wrote /workspace/build/{name}/tool.py "
                f"({n} lines). Run run_tests to check it.]"
            ),
        )

    return RegisteredTool(
        name="write_tool_code",
        description=_WRITE_DESCRIPTION_TEMPLATE.format(name=name),
        input_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "The complete Python source of tool.py — the whole file, "
                        "not a diff or fragment."
                    ),
                },
            },
            "required": ["code"],
        },
        handler=handler,
        trust="TRUSTED",
        serial_group=SANDBOX_SERIAL_GROUP,
    )


def build_run_tests(sandbox: BashSandbox, name: str) -> RegisteredTool:
    """Build ``run_tests`` bound to *sandbox* for the ``build/<name>`` suite.

    Trust follows ``run_bash``'s derivation: pytest executes model-written code,
    so with the network up its output can carry fetched text into context.
    """
    trust: Trust = "UNVERIFIED" if sandbox.network_enabled else "TRUSTED"

    async def handler(inp: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await sandbox.run(pytest_command(name), timeout=TEST_RUN_TIMEOUT)
        if result.timed_out:
            return _error(result.stdout)
        return ToolResult(
            tool_use_id="",
            content=result.stdout,
            is_error=result.exit_code != 0,
        )

    return RegisteredTool(
        name="run_tests",
        description=_RUN_TESTS_DESCRIPTION,
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        trust=trust,
        serial_group=SANDBOX_SERIAL_GROUP,
    )
