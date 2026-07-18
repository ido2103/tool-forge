"""BashSandbox unit tests — command construction, lifecycle, truncation, timeout.

Uses a fake subprocess runner (no Docker); the real-container round-trip lives in
test_bash_live.py behind the ``live`` marker.
"""

from __future__ import annotations

import asyncio

import pytest

from toolforge.config import SandboxSettings
from toolforge.sandbox.bash import BashSandbox, strip_ansi, truncate_output


class FakeRunner:
    """Records argv calls and replays scripted (exit_code, output) results."""

    def __init__(self, results: list[tuple[int | None, bytes] | BaseException]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], *, timeout: int | None) -> tuple[int | None, bytes]:
        self.calls.append(argv)
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _sandbox(runner: FakeRunner, sandbox_settings: SandboxSettings) -> BashSandbox:
    return BashSandbox(sandbox_settings, runner=runner)


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_strip_ansi() -> None:
    assert strip_ansi("\x1b[2mhello\x1b[0m world") == "hello world"


def test_truncate_output_under_cap() -> None:
    text, truncated = truncate_output("short", 100)
    assert text == "short"
    assert truncated is False


def test_truncate_output_over_cap() -> None:
    text, truncated = truncate_output("A" * 50 + "B" * 50, 20)
    assert truncated is True
    assert "output truncated" in text
    assert text.startswith("AAAAAAAAAA")  # head half
    assert text.endswith("BBBBBBBBBB")  # tail half


# ── command construction ─────────────────────────────────────────────────────


async def test_exec_argv_shape(sandbox_settings: SandboxSettings) -> None:
    runner = FakeRunner([(0, b"started"), (0, b"hi\n")])
    sb = _sandbox(runner, sandbox_settings)
    await sb.run("echo hi")
    exec_argv = runner.calls[1]
    assert exec_argv[:3] == ["docker", "exec", sb.container_name]
    assert exec_argv[3:7] == ["bash", "-o", "pipefail", "-lc"]
    assert exec_argv[7] == "echo hi"


async def test_network_none_flag_present(sandbox_settings: SandboxSettings) -> None:
    # sandbox_settings fixture uses network="none"
    runner = FakeRunner([(0, b"started"), (0, b"")])
    sb = _sandbox(runner, sandbox_settings)
    await sb.run("true")
    run_argv = runner.calls[0]
    assert "--network" in run_argv
    assert run_argv[run_argv.index("--network") + 1] == "none"


async def test_network_on_flag_absent(sandbox_settings: SandboxSettings) -> None:
    on = sandbox_settings.model_copy(update={"network": "on"})
    runner = FakeRunner([(0, b"started"), (0, b"")])
    sb = _sandbox(runner, on)
    await sb.run("true")
    assert "--network" not in runner.calls[0]


async def test_run_argv_mounts_workspace(sandbox_settings: SandboxSettings) -> None:
    runner = FakeRunner([(0, b"started"), (0, b"")])
    sb = _sandbox(runner, sandbox_settings)
    await sb.run("true")
    run_argv = runner.calls[0]
    assert "-w" in run_argv
    assert run_argv[run_argv.index("-w") + 1] == "/workspace"
    mount = run_argv[run_argv.index("-v") + 1]
    assert mount.endswith(":/workspace")


# ── lifecycle ────────────────────────────────────────────────────────────────


async def test_lazy_start_only_once(sandbox_settings: SandboxSettings) -> None:
    runner = FakeRunner([(0, b"started"), (0, b"a"), (0, b"b")])
    sb = _sandbox(runner, sandbox_settings)
    await sb.run("echo a")
    await sb.run("echo b")
    # 1 docker run + 2 docker exec = 3 calls; only one "run" (start).
    starts = [c for c in runner.calls if c[:2] == ["docker", "run"]]
    assert len(starts) == 1
    assert len(runner.calls) == 3


async def test_start_failure_raises(sandbox_settings: SandboxSettings) -> None:
    runner = FakeRunner([(1, b"no such image")])
    sb = _sandbox(runner, sandbox_settings)
    with pytest.raises(RuntimeError, match="failed to start sandbox container"):
        await sb.run("true")


async def test_teardown_idempotent_before_start(sandbox_settings: SandboxSettings) -> None:
    runner = FakeRunner([])
    sb = _sandbox(runner, sandbox_settings)
    sb.teardown()  # never started → no-op, must not raise
    sb.teardown()


# ── run() output handling ────────────────────────────────────────────────────


async def test_run_strips_ansi_and_reports_exit_code(sandbox_settings: SandboxSettings) -> None:
    runner = FakeRunner([(0, b"started"), (0, b"\x1b[32mgreen\x1b[0m\n")])
    sb = _sandbox(runner, sandbox_settings)
    result = await sb.run("echo green")
    assert result.stdout == "green\n"
    assert result.exit_code == 0
    assert result.timed_out is False


async def test_run_over_cap_truncates(sandbox_settings: SandboxSettings) -> None:
    small = sandbox_settings.model_copy(update={"output_cap": 20})
    runner = FakeRunner([(0, b"started"), (0, b"X" * 100)])
    sb = _sandbox(runner, small)
    result = await sb.run("yes X")
    assert result.truncated is True
    assert "output truncated" in result.stdout


async def test_run_timeout_returns_timed_out(sandbox_settings: SandboxSettings) -> None:
    runner = FakeRunner([(0, b"started"), TimeoutError()])
    sb = _sandbox(runner, sandbox_settings)
    result = await sb.run("sleep 999", timeout=5)
    assert result.timed_out is True
    assert result.exit_code is None
    assert "timed out after 5s" in result.stdout


async def test_run_cancel_propagates(sandbox_settings: SandboxSettings) -> None:
    # CancelledError from the runner must propagate (loop's cancel path relies on it).
    runner = FakeRunner([(0, b"started"), asyncio.CancelledError()])
    sb = _sandbox(runner, sandbox_settings)
    with pytest.raises(asyncio.CancelledError):
        await sb.run("sleep 999")
