"""ForgeWorker unit tests — attempt loop, pristine verification, escalation.

Uses the scripted FakeProviderClient (no API) and FakeRunner (no Docker); the
real round trip lives in test_worker_live.py behind the ``live`` marker.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.orchestrator._harness import FakeProviderClient, assistant_text, assistant_tool_use
from tests.sandbox.test_bash import FakeRunner
from toolforge.config import SandboxSettings, WorkerSettings
from toolforge.forge.candidates import ToolSpec
from toolforge.forge.test_author import AuthoredTests
from toolforge.forge.worker import BuildResult, ForgeWorker, WorkerError
from toolforge.providers import Message, PermanentProviderError
from toolforge.sandbox import BashSandbox

# ── scripted materials ───────────────────────────────────────────────────────

SUITE = """\
from tool import run


def test_a():
    assert run(text="A") == "a"
"""

GOOD_CODE = "def run(text):\n    return text.lower()\n"

GREEN = (
    0,
    b"test_tool.py::test_a PASSED\n"
    b"test_tool.py::test_b PASSED\n"
    b"test_tool.py::test_c PASSED\n"
    b"test_tool.py::test_d PASSED\n"
    b"test_tool.py::test_e PASSED\n"
    b"5 passed in 0.03s\n",
)
RED = (
    1,
    b"test_tool.py::test_a FAILED\ntest_tool.py::test_b PASSED\n1 failed, 4 passed in 0.03s\n",
)
GREEN_PARTIAL = (
    0,
    b"test_tool.py::test_a PASSED\n"
    b"test_tool.py::test_b PASSED\n"
    b"test_tool.py::test_c PASSED\n"
    b"3 passed in 0.02s\n",
)


def write_reply() -> Message:
    return assistant_tool_use(("tu_1", "write_tool_code", {"code": GOOD_CODE}))


def done_reply() -> Message:
    return assistant_text("run_tests is green — done.")


@pytest.fixture
def worker_settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        backend="api",
        api_model="worker-test",
        max_attempts=2,
        max_iterations=5,
        max_tokens=2048,
        timeout_seconds=100,
    )


@pytest.fixture
def spec() -> ToolSpec:
    return ToolSpec(
        name="slugify",
        description="Turn text into a URL slug.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "text to slugify"}},
            "required": ["text"],
        },
        behavior="Lowercase the input.",
    )


@pytest.fixture
def tests_fixture(sandbox_settings: SandboxSettings) -> AuthoredTests:
    build_dir = sandbox_settings.workspace_path / "build" / "slugify"
    build_dir.mkdir(parents=True)
    (build_dir / "test_tool.py").write_text(SUITE, encoding="utf-8")
    return AuthoredTests(
        test_path="/workspace/build/slugify/test_tool.py",
        test_count=5,
        report="5 failed (stub run)",
        attempts=1,
    )


def make_worker(
    script: list[Message | Exception],
    results: list[tuple[int | None, bytes]],
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    *,
    client: FakeProviderClient | None = None,
    runs_dir: Path | None = None,
) -> tuple[ForgeWorker, FakeProviderClient, FakeRunner]:
    client = client if client is not None else FakeProviderClient(script)
    runner = FakeRunner([(0, b"started"), *results])
    sandbox = BashSandbox(sandbox_settings, runner=runner)
    worker = ForgeWorker(
        client,
        sandbox,
        sandbox_settings,
        worker_settings,
        model="worker-test",
        runs_dir=runs_dir,
    )
    return worker, client, runner


def build_dir(sandbox_settings: SandboxSettings) -> Path:
    return sandbox_settings.workspace_path / "build" / "slugify"


# ── happy path ───────────────────────────────────────────────────────────────


async def test_happy_path(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    worker, client, _ = make_worker(
        [write_reply(), done_reply()], [GREEN], sandbox_settings, worker_settings
    )
    result = await worker.build(spec, tests_fixture)

    assert isinstance(result, BuildResult)
    assert result.code == GOOD_CODE
    assert result.code_path == "/workspace/build/slugify/tool.py"
    assert result.test_path == tests_fixture.test_path
    assert "5 passed" in result.test_report
    assert result.attempts == 1
    assert (build_dir(sandbox_settings) / "tool.py").read_text() == GOOD_CODE
    for call in client.calls:
        assert call["component"] == "forge_worker"
        assert call["model"] == "worker-test"


async def test_transcript_written(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    worker, _, _ = make_worker(
        [write_reply(), done_reply()],
        [GREEN],
        sandbox_settings,
        worker_settings,
        runs_dir=runs,
    )
    await worker.build(spec, tests_fixture)
    files = list(runs.glob("forge-slugify-*.jsonl"))
    assert len(files) == 1
    assert files[0].read_text().strip()  # at least the brief + replies were mirrored


# ── red then green ───────────────────────────────────────────────────────────


async def test_red_then_green_feeds_pytest_output_back(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    worker, client, _ = make_worker(
        [write_reply(), done_reply(), write_reply(), done_reply()],
        [RED, GREEN],
        sandbox_settings,
        worker_settings,
    )
    result = await worker.build(spec, tests_fixture)
    assert result.attempts == 2

    # The second attempt's first send carries the harness feedback as the
    # latest user message.
    feedback_call = client.calls[2]
    feedback_text = feedback_call["messages"][-1].text
    assert "[Attempt 1 of 2]" in feedback_text
    assert "FAILED" in feedback_text
    assert "Fix tool.py" in feedback_text


# ── exhaustion ───────────────────────────────────────────────────────────────


async def test_exhaustion_raises_with_last_report_and_keeps_artifacts(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    worker, _, _ = make_worker(
        [write_reply(), done_reply(), write_reply(), done_reply()],
        [RED, RED],
        sandbox_settings,
        worker_settings,
    )
    with pytest.raises(WorkerError, match="after 2 verification attempts") as excinfo:
        await worker.build(spec, tests_fixture)
    assert "FAILED" in str(excinfo.value)
    # Near-miss artifacts stay for orchestrator inspection.
    assert (build_dir(sandbox_settings) / "test_tool.py").exists()
    assert (build_dir(sandbox_settings) / "tool.py").exists()


# ── verification: exact-green grading ────────────────────────────────────────


async def test_partial_green_is_red(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    # Exit 0 with only 3 of 5 authored tests seen passing must not count.
    worker, _, _ = make_worker(
        [write_reply(), done_reply(), write_reply(), done_reply()],
        [GREEN_PARTIAL, GREEN_PARTIAL],
        sandbox_settings,
        worker_settings,
    )
    with pytest.raises(WorkerError):
        await worker.build(spec, tests_fixture)


# ── tampering ────────────────────────────────────────────────────────────────


class TamperOnFirstSendClient(FakeProviderClient):
    """Simulates a worker that edits protected files during its first turn."""

    def __init__(self, script: list[Message | Exception], paths: list[Path]) -> None:
        super().__init__(script)
        self._paths = list(paths)

    async def send(self, **kwargs: Any) -> Message:
        for path in self._paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# tampered\n", encoding="utf-8")
        self._paths.clear()
        return await super().send(**kwargs)


async def test_tamper_fails_attempt_even_when_green(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    bd = build_dir(sandbox_settings)
    workspace = sandbox_settings.workspace_path
    client = TamperOnFirstSendClient(
        [write_reply(), done_reply(), write_reply(), done_reply()],
        [bd / "test_tool.py", bd / "conftest.py", workspace / "conftest.py"],
    )
    worker, _, _ = make_worker([], [GREEN, GREEN], sandbox_settings, worker_settings, client=client)
    result = await worker.build(spec, tests_fixture)

    # Attempt 1 was green but tampered → failed; attempt 2 (no tampering) wins.
    assert result.attempts == 2
    # Pristine suite restored, config files swept.
    assert (bd / "test_tool.py").read_text() == SUITE
    assert not (bd / "conftest.py").exists()
    assert not (workspace / "conftest.py").exists()
    # The feedback names the restored/removed files.
    feedback_text = client.calls[2]["messages"][-1].text
    assert "restored or removed" in feedback_text
    assert "build/slugify/test_tool.py" in feedback_text
    assert "conftest.py" in feedback_text


# ── cancellation, deadline, provider errors ──────────────────────────────────


class CancelOnSendClient(FakeProviderClient):
    """Raises CancelledError on send — a BaseException the script list can't carry."""

    async def send(self, **kwargs: Any) -> Message:
        raise asyncio.CancelledError


async def test_interrupted_inner_run_raises_cancelled(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    # The inner loop converts a mid-send CancelledError into a "Stopping."
    # message with stop_reason="interrupted"; the driver must re-raise.
    worker, _, _ = make_worker(
        [], [], sandbox_settings, worker_settings, client=CancelOnSendClient([])
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.build(spec, tests_fixture)


async def test_deadline_exceeded_raises_worker_error(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    worker, _, _ = make_worker([], [], sandbox_settings, worker_settings)
    clock_values = iter([0.0, 1_000.0])
    worker._clock = lambda: next(clock_values)
    with pytest.raises(WorkerError, match="timed out"):
        await worker.build(spec, tests_fixture)


async def test_provider_error_wrapped(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
    tests_fixture: AuthoredTests,
) -> None:
    worker, _, _ = make_worker(
        [PermanentProviderError("boom")], [], sandbox_settings, worker_settings
    )
    with pytest.raises(WorkerError, match="provider error during build"):
        await worker.build(spec, tests_fixture)


async def test_missing_suite_raises_worker_error(
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    spec: ToolSpec,
) -> None:
    worker, _, _ = make_worker([], [], sandbox_settings, worker_settings)
    tests = AuthoredTests(
        test_path="/workspace/build/slugify/test_tool.py",
        test_count=5,
        report="",
        attempts=1,
    )
    with pytest.raises(WorkerError, match="authored suite unreadable"):
        await worker.build(spec, tests)
