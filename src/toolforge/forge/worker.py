"""The forge worker — spec + red suite → a green ``tool.py``, or a failure log.

The second stage of the forge's build loop: an agentic worker (a cheaper,
different model from the orchestrator/test author — the cross-model invariant)
implements ``/workspace/build/<name>/tool.py`` against the authored suite. The
worker's agent loop is the orchestrator's own :class:`Orchestrator` class,
instantiated with a private three-tool registry (``run_bash``,
``write_tool_code``, ``run_tests``) and ``component="forge_worker"``; a thin
driver runs the outer attempt loop in evaluator-optimizer shape: worker run →
**harness-authoritative verification** → green: done; red: feed the pytest
output back as the next user message on the same history.

Anti-reward-hack: the authored suite is captured in driver memory (host
process — never on disk where the worker could find it) before the worker's
first turn. Every verification restores it, sweeps pytest config files
(``conftest.py``/ini files in the build dir, ``build/``, and the workspace
root — pytest walks ancestor directories for config discovery, and nothing
legitimate writes pytest config to the workspace root), reruns the suite, and
requires *exactly* the authored test count to pass. The worker's own
``run_tests`` calls are advisory; only the harness run counts, and any
tampering fails the attempt even if the restored suite passes.

The authoritative run executes in a **fresh, throwaway container per
verification** — never the session's shared container. The worker's
``run_bash`` has unrestricted shell access to the shared container, so
anything there (the ``python3``/``pytest`` binaries, ``sitecustomize.py``,
shell rc files) must be presumed rigged; only ``/workspace`` state is
restorable host-side. A container started fresh from the image cannot carry
any of that, so the verification's stdout is genuinely pytest's.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from toolforge.config import SandboxSettings, WorkerSettings
from toolforge.forge.candidates import ToolSpec
from toolforge.forge.test_author import (
    ENSURE_PYTEST_CMD,
    INSTALL_TIMEOUT,
    AuthoredTests,
    parse_stub_run,
)
from toolforge.forge.worker_tools import (
    TEST_RUN_TIMEOUT,
    build_run_tests,
    build_write_tool_code,
    pytest_command,
)
from toolforge.orchestrator.hooks import HookManager
from toolforge.orchestrator.loop import Orchestrator
from toolforge.orchestrator.transcript import Transcript
from toolforge.providers import Message, ProviderClient, ProviderError
from toolforge.registry import ToolContext, ToolRegistry
from toolforge.sandbox import BashSandbox, truncate_output
from toolforge.sandbox.run_bash import build_run_bash

_REPORT_CAP = 8_000

# Files pytest reads for configuration/fixtures; any of them can skip or
# monkeypatch the whole suite, so the verification sweeps them away.
_PYTEST_CONFIG_FILES = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")

_WORKER_SYSTEM_TEMPLATE = """\
You are the forge worker inside Toolforge. A tool spec and a pytest suite \
written from it arrive; your job is to implement /workspace/build/{name}/tool.py \
so the whole suite passes.

The implementation contract:
- tool.py is a plain Python module defining run(...) whose keyword parameters \
exactly match the spec's input_schema properties.
- run() returns a string or a JSON-serializable value; failures follow the \
spec's behavior contract.
- Standard library only: the runtime container is minimal and rebuilt between \
sessions, so a third-party import would break the tool later even when tests \
pass now. write_tool_code rejects non-stdlib imports mechanically.

Work in this order:
1. Read the tests and the behavior contract first. Before any code, state \
briefly: the exact run() signature, the edge cases the tests encode, and the \
error contract they demand.
2. Write the complete file with write_tool_code (always the whole file, never \
a fragment).
3. Run run_tests and read each failing assertion before editing anything.
4. Repeat steps 2-3, changing tool.py between runs. Use run_bash for quick \
experiments (python3 -c ...) when a failure is confusing.
5. When run_tests is fully green — or you are genuinely stuck — say so in \
plain text and stop; the harness then runs the official verification.

Test integrity:
- The tests are the specification. The harness keeps its own pristine copy of \
test_tool.py and verifies against it after you finish — only that run counts. \
Editing test_tool.py, adding conftest.py, or configuring pytest cannot make \
the build succeed and wastes your budget.
- If you believe a test contradicts the spec, satisfy the spec as best you can \
and report the conflicting test by name in your final message instead of \
working around it.

Budget: you have {max_attempts} verification rounds and a limited number of \
turns per round. Be economical: complete-file writes, one test run per change, \
no exploratory detours."""


@dataclass
class BuildResult:
    """A harness-verified green build, ready to become a Candidate."""

    code: str  # final tool.py source
    code_path: str  # container path, e.g. "/workspace/build/<name>/tool.py"
    test_path: str  # container path of the authored suite
    test_report: str  # trimmed output of the authoritative green run
    attempts: int  # harness verifications consumed


@dataclass
class _Verification:
    """One authoritative harness run: outcome, evidence, and tamper record."""

    green: bool
    report: str
    tampered: list[str]


class WorkerError(Exception):
    """Build failed — budget/deadline exhausted or the environment is unusable.

    The message carries the last verification log so ``forge_tool`` can
    escalate a useful failure record to the orchestrator.
    """


# ── rendering ────────────────────────────────────────────────────────────────


def _render_build_brief(spec: ToolSpec, tests: AuthoredTests, suite_source: str) -> str:
    lines = [
        "Implement the tool for this spec.",
        "",
        "## Tool name",
        spec.name,
        "",
        "## Description",
        spec.description,
        "",
        "## Input schema",
        json.dumps(spec.input_schema, indent=2),
        "",
        "## Behavior contract",
        spec.behavior,
    ]
    if spec.examples:
        lines += ["", "## Examples (input → exact output)"]
        for ex in spec.examples:
            lines.append(f"- input: {json.dumps(ex.get('input'))} → output: {ex.get('output')!r}")
    if spec.allowed_domains:
        lines += [
            "",
            f"Note: at runtime this tool may reach the network ({', '.join(spec.allowed_domains)}) "
            "via urllib.request, per the behavior contract. The tests are fully offline and "
            "cover only offline-verifiable behavior.",
        ]
    lines += [
        "",
        f"## The test suite you must pass (/workspace/build/{spec.name}/test_tool.py)",
        "```python",
        suite_source.rstrip("\n"),
        "```",
        "",
        f"All {tests.test_count} tests must pass. Begin with your brief analysis, "
        "then write tool.py.",
    ]
    return "\n".join(lines)


def _render_feedback(attempt: int, max_attempts: int, verification: _Verification) -> str:
    parts = [f"[Attempt {attempt} of {max_attempts}] The harness verification failed."]
    if verification.tampered:
        names = ", ".join(verification.tampered)
        parts.append(
            f"Files you are not allowed to change were modified and have been restored "
            f"or removed: {names}. The tests are the specification — the harness always "
            "verifies against its pristine copy, so this can only waste your remaining "
            "attempts. Fix tool.py instead."
        )
    parts.append(verification.report)
    parts.append("Fix tool.py and finish when run_tests is green.")
    return "\n\n".join(parts)


def _forge_run_path(runs_dir: Path, name: str) -> Path:
    """A fresh UTC-stamped worker-transcript path (``forge-<name>-<ts>.jsonl``)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return runs_dir / f"forge-{name}-{stamp}.jsonl"


# ── the worker ───────────────────────────────────────────────────────────────


class ForgeWorker:
    """Drives the build→verify→feedback loop for one tool spec at a time."""

    def __init__(
        self,
        client: ProviderClient,
        sandbox: BashSandbox,
        sandbox_settings: SandboxSettings,
        settings: WorkerSettings,
        *,
        model: str,
        hooks: HookManager | None = None,
        runs_dir: Path | None = None,
        verify_sandbox_factory: Callable[[], BashSandbox] | None = None,
    ) -> None:
        self._client = client
        self._sandbox = sandbox
        self._sandbox_settings = sandbox_settings
        self._settings = settings
        self._model = model
        self._hooks = hooks
        self._runs_dir = runs_dir
        # The authoritative verification runs in a fresh, throwaway container
        # (never the shared one the worker can rig via run_bash); the factory
        # is a seam so unit tests can substitute a fake runner.
        self._verify_sandbox_factory = verify_sandbox_factory or (
            lambda: BashSandbox(sandbox_settings)
        )
        # Seam for deadline tests; production always uses the monotonic clock.
        self._clock: Callable[[], float] = time.monotonic

    async def build(self, spec: ToolSpec, tests: AuthoredTests) -> BuildResult:
        """Implement tool.py until the pristine suite passes; raise WorkerError on failure."""
        deadline = self._clock() + self._settings.timeout_seconds
        build_dir = self._sandbox_settings.workspace_path / "build" / spec.name
        suite_file = build_dir / "test_tool.py"
        try:
            pristine_suite = suite_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkerError(f"authored suite unreadable at {suite_file}: {exc}") from exc

        registry = ToolRegistry(ToolContext())
        registry.register(build_run_bash(self._sandbox))
        registry.register(build_write_tool_code(build_dir, spec.name))
        registry.register(build_run_tests(self._sandbox, spec.name))

        transcript = (
            Transcript(_forge_run_path(self._runs_dir, spec.name))
            if self._runs_dir is not None
            else None
        )
        inner = Orchestrator(
            client=self._client,
            registry=registry,
            hooks=self._hooks or HookManager(),
            model=self._model,
            max_tokens=self._settings.max_tokens,
            max_iterations=self._settings.max_iterations,
            transcript=transcript,
            component="forge_worker",
        )
        system = _WORKER_SYSTEM_TEMPLATE.format(
            name=spec.name, max_attempts=self._settings.max_attempts
        )
        history: list[Message] = []
        next_message = _render_build_brief(spec, tests, pristine_suite)
        last_report = "no verification ran"

        for attempt in range(1, self._settings.max_attempts + 1):
            self._check_deadline(deadline, "worker run")
            try:
                await inner.run(next_message, history, system_prompt=system)
            except ProviderError as exc:
                # The adapter (and the inner loop's long-pause retry) already
                # ran; convert so the caller sees the documented error type.
                raise WorkerError(
                    f"provider error during build (attempt {attempt}): {exc}"
                ) from exc
            # The inner loop converts mid-send cancellation into a normal
            # "Stopping." return; surface it so the outer loop's cancel path
            # (synthesized [ABORTED] results) runs instead of a verification.
            if history and history[-1].stop_reason == "interrupted":
                raise asyncio.CancelledError

            self._check_deadline(deadline, "verification")
            verification = await self._verify(build_dir, pristine_suite, tests.test_count)
            last_report = verification.report
            if verification.green:
                code = (build_dir / "tool.py").read_text(encoding="utf-8")
                return BuildResult(
                    code=code,
                    code_path=f"/workspace/build/{spec.name}/tool.py",
                    test_path=tests.test_path,
                    test_report=verification.report,
                    attempts=attempt,
                )
            next_message = _render_feedback(attempt, self._settings.max_attempts, verification)

        # Deliberately leave build/<name>/ in place: the near-miss artifacts
        # help the orchestrator write a better spec (unlike the test author's
        # cleanup — its failures leave nothing worth inspecting).
        raise WorkerError(
            f"tests still failing after {self._settings.max_attempts} verification "
            f"attempts; last run:\n{last_report}"
        )

    async def _verify(self, build_dir: Path, pristine_suite: str, test_count: int) -> _Verification:
        """The one test run that counts: restore, sweep, run, and grade exactly."""
        workspace = self._sandbox_settings.workspace_path
        tampered: list[str] = []

        build_dir.mkdir(parents=True, exist_ok=True)
        suite_file = build_dir / "test_tool.py"
        current = suite_file.read_text(encoding="utf-8") if suite_file.exists() else None
        if current != pristine_suite:
            tampered.append(str(suite_file.relative_to(workspace)))
            suite_file.write_text(pristine_suite, encoding="utf-8")

        for directory in (build_dir, build_dir.parent, workspace):
            for fname in _PYTEST_CONFIG_FILES:
                config = directory / fname
                if config.exists():
                    config.unlink()
                    tampered.append(str(config.relative_to(workspace)))

        # The one run that counts happens in a container started fresh from
        # the image: nothing the worker did to the shared container (rigged
        # binaries, sitecustomize, shell rc) can exist there. Only /workspace
        # is carried over — and that is exactly the state restored above.
        verify_sandbox = self._verify_sandbox_factory()
        try:
            install = await verify_sandbox.run(ENSURE_PYTEST_CMD, timeout=INSTALL_TIMEOUT)
            if install.timed_out or install.exit_code != 0:
                raise WorkerError(
                    "pytest could not be installed in the verification container "
                    f"(is the sandbox offline?): {install.stdout}"
                )
            result = await verify_sandbox.run(
                pytest_command(build_dir.name), timeout=TEST_RUN_TIMEOUT
            )
        finally:
            verify_sandbox.teardown()
        report, _ = truncate_output(result.stdout, _REPORT_CAP)
        if result.timed_out:
            return _Verification(green=False, report=report, tampered=tampered)

        outcome = parse_stub_run(result.stdout)
        # Exactly green: exit 0 alone is spoofable (os._exit(0), partial
        # collection); every authored test must be seen to PASS.
        green = (
            result.exit_code == 0
            and len(outcome.passed) == test_count
            and not outcome.failed
            and not outcome.errors
            and not tampered
        )
        return _Verification(green=green, report=report, tampered=tampered)

    def _check_deadline(self, deadline: float, step: str) -> None:
        if self._clock() >= deadline:
            raise WorkerError(
                f"build timed out after {self._settings.timeout_seconds}s (before the {step})"
            )
