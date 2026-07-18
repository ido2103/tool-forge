"""Adversarial test author — spec → verified-red pytest suite in the workspace.

The first stage of the forge's build loop: a frontier-tier model turns a
:class:`~toolforge.forge.candidates.ToolSpec` into a pytest file at
``/workspace/build/<name>/test_tool.py``, written before any implementation
exists (TDD). The suite is only accepted after mechanical validation in the
sandbox: it must collect cleanly, contain at least ``min_tests`` tests, and
every test must FAIL against a stub ``tool.py`` whose ``run()`` raises
``NotImplementedError`` — a test that passes against that stub asserts nothing
about real behavior and would hand the worker a vacuous target.

Model output is a single fenced ```python block preceded by an edge-case
analysis: the analysis-first protocol forces the reasoning to be generated
before the code that should follow from it, and plain fenced source avoids the
accuracy tax of JSON-escaped code. Validation failures are fed back into the
same conversation (fix-in-context, not blind regeneration) under a bounded,
config-driven attempt budget and an overall wall-clock deadline
(``TestAuthorSettings``). Files are written host-side like ``promote.py`` does
— the bind mount makes them visible in the container without shell quoting
hazards; only pytest execution goes through the sandbox.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from toolforge.config import SandboxSettings, TestAuthorSettings
from toolforge.forge.candidates import ToolSpec
from toolforge.providers import Message, ProviderClient, ProviderError, TextBlock
from toolforge.sandbox import BashSandbox, truncate_output

_PYTHON_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)
# Modules that break the offline/deterministic contract. `http` also covers
# http.client; `subprocess` closes the shell-out escape hatch. This screen is
# what keeps authored tests offline: at forge time the sandbox network is ON
# (pip needs it), so the container is not enforcing isolation here.
_BANNED_MODULES = frozenset({"socket", "urllib", "http", "requests", "subprocess"})
_IMPORT_LINE_RE = re.compile(r"^\s*(import|from)\s+(.+)$", re.MULTILINE)
_COLLECT_COUNT_RE = re.compile(r"(\d+) tests? collected")
_TEST_RESULT_RE = re.compile(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR)", re.MULTILINE)

_REPORT_CAP = 8_000
_INSTALL_TIMEOUT = 240
_COLLECT_TIMEOUT = 60
_STUB_RUN_TIMEOUT = 120

_STUB_TOOL = """\
def run(**kwargs):
    raise NotImplementedError("tool not implemented yet")
"""

# Ensure pytest is importable in the container; python:3.12-slim ships without
# it. The || arm only runs on a cold container, so the check stays cheap.
_ENSURE_PYTEST_CMD = (
    "python3 -m pytest --version >/dev/null 2>&1 || python3 -m pip install --quiet pytest"
)

_AUTHOR_SYSTEM_TEMPLATE = """\
You are the adversarial test author inside Toolforge's forge. A tool spec \
arrives from the orchestrator; a separate, weaker model will later implement \
the tool. You write the pytest suite that implementation must pass — from the \
spec alone, before any implementation exists. Your tests are the spec's teeth: \
an implementation that merely looks plausible must fail them, so prefer exact \
assertions over weak ones (assert the full return value, not just its type).

The implementation contract:
- The tool will be `tool.py` in the same directory as your test file, exposing \
run(...) whose keyword arguments exactly match the spec's input_schema \
properties. Import it as: from tool import run
- run() returns a value (a string or something JSON-serializable); failures \
follow the spec's behavior contract.

Hard constraints — violations are rejected mechanically, not negotiated:
- Use pytest and the Python 3.12 standard library only.
- Tests must be fully offline and deterministic: no socket, urllib, http, \
requests, or subprocess; no dependence on wall-clock time or unseeded \
randomness. If the spec involves network behavior, test only what is \
verifiable offline: argument validation, the error contract, output shaping.
- Touch the filesystem only through pytest's tmp_path fixture, if at all.
- Every test must call run(...) and assert on behavior the spec promises. \
Your suite will first execute against a stub tool.py whose run() only raises \
NotImplementedError: every test must fail at that point. A test that passes \
against that stub asserts nothing and will be rejected.
- Write at least {min_tests} test functions.

Work in this order:
1. Under a heading "## Edge-case analysis", enumerate in numbered prose the \
behaviors worth testing: each spec example verbatim, boundary values implied \
by each input_schema property's type, plausible-but-malformed inputs and what \
the behavior contract requires for each, and the exact output format.
2. Then emit the complete test file as exactly one fenced code block starting \
with ```python. Give each test function a comment mapping it to a numbered \
case. Emit no other fenced code block in your reply."""


@dataclass
class AuthoredTests:
    """A validated, verified-red suite ready for the worker to implement against."""

    test_path: str  # container path, e.g. "/workspace/build/<name>/test_tool.py"
    test_count: int
    report: str  # trimmed stub-run output: the red-suite evidence
    attempts: int


@dataclass
class StubRunOutcome:
    """Per-test results parsed from a ``pytest -v`` run against the stub."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class TestAuthorError(Exception):
    """Authoring failed — budget/deadline exhausted or the sandbox is unusable."""

    # The Test* name matches pytest's collection convention; opt out explicitly.
    __test__: ClassVar[bool] = False


# ── pure helpers ─────────────────────────────────────────────────────────────


def extract_python_block(text: str) -> str | None:
    """Return the reply's single fenced python block, or None if not exactly one."""
    blocks = _PYTHON_BLOCK_RE.findall(text)
    if len(blocks) != 1:
        return None
    source: str = blocks[0]
    return source


def _banned_imports(source: str) -> list[str]:
    """Banned root modules imported anywhere, including ``import a, b`` lists."""
    found = []
    for keyword, rest in _IMPORT_LINE_RE.findall(source):
        # `from x import y` names one module; `import a, b as c` names several.
        names = [rest] if keyword == "from" else rest.split(",")
        for name in names:
            parts = name.split()
            root = parts[0].split(".")[0] if parts else ""
            if root in _BANNED_MODULES:
                found.append(root)
    return found


def screen_test_source(source: str) -> list[str]:
    """Static screen before anything runs; returns problems, empty when clean."""
    problems = [
        f"forbidden import '{module}' (tests must be offline and deterministic)"
        for module in _banned_imports(source)
    ]
    if "from tool import run" not in source:
        problems.append("missing 'from tool import run' — tests must exercise the tool module")
    if not re.search(r"^def test_", source, re.MULTILINE):
        problems.append("no top-level 'def test_...' functions found")
    return problems


def parse_collect_count(output: str) -> int | None:
    m = _COLLECT_COUNT_RE.search(output)
    return int(m.group(1)) if m else None


def parse_stub_run(output: str) -> StubRunOutcome:
    outcome = StubRunOutcome()
    buckets = {"PASSED": outcome.passed, "FAILED": outcome.failed, "ERROR": outcome.errors}
    for m in _TEST_RESULT_RE.finditer(output):
        buckets[m.group(2)].append(m.group(1))
    return outcome


def _render_spec(spec: ToolSpec) -> str:
    lines = [
        "Write the adversarial pytest suite for this tool spec.",
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
            f"Note: at runtime this tool may reach the network ({', '.join(spec.allowed_domains)}), "
            "but your tests must not. Test only offline-verifiable behavior: argument "
            "validation, the error contract, output shaping.",
        ]
    return "\n".join(lines)


def _user_message(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)], ts=datetime.now(tz=UTC))


# ── the author ───────────────────────────────────────────────────────────────


class TestAuthor:
    """Drives the author→validate→feedback loop for one tool spec at a time."""

    # The Test* name matches pytest's collection convention; opt out explicitly.
    __test__: ClassVar[bool] = False

    def __init__(
        self,
        client: ProviderClient,
        sandbox: BashSandbox,
        sandbox_settings: SandboxSettings,
        settings: TestAuthorSettings,
        *,
        model: str,
    ) -> None:
        self._client = client
        self._sandbox = sandbox
        self._sandbox_settings = sandbox_settings
        self._settings = settings
        self._model = model
        # Seam for deadline tests; production always uses the monotonic clock.
        self._clock: Callable[[], float] = time.monotonic

    async def author_tests(self, spec: ToolSpec) -> AuthoredTests:
        """Author and validate a red suite for *spec*; raise TestAuthorError on failure."""
        deadline = self._clock() + self._settings.timeout_seconds
        build_dir = self._sandbox_settings.workspace_path / "build" / spec.name
        try:
            return await self._author(spec, build_dir, deadline)
        except TestAuthorError:
            shutil.rmtree(build_dir, ignore_errors=True)
            raise

    async def _author(self, spec: ToolSpec, build_dir: Path, deadline: float) -> AuthoredTests:
        install = await self._sandbox.run(_ENSURE_PYTEST_CMD, timeout=_INSTALL_TIMEOUT)
        if install.timed_out or install.exit_code != 0:
            raise TestAuthorError(
                "pytest could not be installed in the sandbox (is the container "
                f"offline?): {install.stdout}"
            )

        system = _AUTHOR_SYSTEM_TEMPLATE.format(min_tests=self._settings.min_tests)
        transcript = [_user_message(_render_spec(spec))]
        cd = f"cd /workspace/build/{spec.name}"
        last_failure = "no attempts made"

        for attempt in range(1, self._settings.max_attempts + 1):
            self._check_deadline(deadline, "model call")
            try:
                reply = await self._client.send(
                    messages=transcript,
                    system=system,
                    model=self._model,
                    max_tokens=self._settings.max_tokens,
                    component="test_author",
                )
            except ProviderError as exc:
                # The adapter already ran its internal retry ladder; convert so
                # the caller sees the documented error type and cleanup runs.
                raise TestAuthorError(
                    f"provider error during authoring (attempt {attempt}): {exc}"
                ) from exc
            transcript.append(reply)

            def feedback(text: str) -> None:
                nonlocal last_failure
                last_failure = text
                transcript.append(_user_message(text))

            source = extract_python_block(reply.text)
            if source is None:
                n = len(_PYTHON_BLOCK_RE.findall(reply.text))
                feedback(
                    f"Your reply contained {n} fenced ```python blocks; exactly one is "
                    "required. Re-emit the complete test file as a single block."
                )
                continue

            problems = screen_test_source(source)
            if problems:
                feedback(
                    "Rejected before running: "
                    + "; ".join(problems)
                    + ". Fix and re-emit the complete file."
                )
                continue

            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "test_tool.py").write_text(source, encoding="utf-8")
            (build_dir / "tool.py").write_text(_STUB_TOOL, encoding="utf-8")

            self._check_deadline(deadline, "collect run")
            collect = await self._sandbox.run(
                f"{cd} && python3 -m pytest --collect-only -q -p no:cacheprovider test_tool.py",
                timeout=_COLLECT_TIMEOUT,
            )
            if collect.timed_out or collect.exit_code != 0:
                feedback(
                    "pytest could not collect your tests:\n"
                    f"{collect.stdout}\nFix and re-emit the complete file."
                )
                continue
            collected = parse_collect_count(collect.stdout) or 0
            if collected < self._settings.min_tests:
                feedback(
                    f"Only {collected} tests collected; at least "
                    f"{self._settings.min_tests} are required. Add cases from your own "
                    "edge-case analysis and re-emit the complete file."
                )
                continue

            self._check_deadline(deadline, "stub run")
            stub_run = await self._sandbox.run(
                f"{cd} && python3 -m pytest -v --tb=line -p no:cacheprovider test_tool.py",
                timeout=_STUB_RUN_TIMEOUT,
            )
            if stub_run.timed_out:
                feedback(
                    f"The suite exceeded {_STUB_RUN_TIMEOUT}s running against the stub — "
                    "remove sleeps and unbounded loops; re-emit the complete file."
                )
                continue
            outcome = parse_stub_run(stub_run.stdout)
            if stub_run.exit_code != 1 or outcome.passed or outcome.errors:
                if outcome.passed:
                    names = ", ".join(outcome.passed)
                    feedback(
                        f"These tests PASSED against a stub whose run() only raises "
                        f"NotImplementedError, so they assert nothing about real "
                        f"behavior: {names}. Rewrite each to call run() and assert on "
                        "spec'd behavior; re-emit the complete file."
                    )
                elif outcome.errors:
                    names = ", ".join(outcome.errors)
                    feedback(
                        f"These tests ERRORED before exercising run() (a broken "
                        f"fixture or a bug inside the test itself), so no "
                        f"implementation could ever satisfy them: {names}. Fix them "
                        "to fail through run()'s behavior; re-emit the complete file."
                    )
                else:
                    feedback(
                        "Running the suite against the stub did not produce a clean "
                        f"all-failing run (pytest exit code {stub_run.exit_code}):\n"
                        f"{stub_run.stdout}\nFix and re-emit the complete file."
                    )
                continue

            # Success: a collected, all-red suite. Remove the stub so a
            # NotImplementedError placeholder can never be mistaken for a build.
            (build_dir / "tool.py").unlink(missing_ok=True)
            report, _ = truncate_output(stub_run.stdout, _REPORT_CAP)
            return AuthoredTests(
                test_path=f"/workspace/build/{spec.name}/test_tool.py",
                test_count=collected,
                report=report,
                attempts=attempt,
            )

        raise TestAuthorError(
            f"test authoring failed after {self._settings.max_attempts} attempts; "
            f"last failure: {last_failure}"
        )

    def _check_deadline(self, deadline: float, step: str) -> None:
        if self._clock() >= deadline:
            raise TestAuthorError(
                f"authoring timed out after {self._settings.timeout_seconds}s (before the {step})"
            )
