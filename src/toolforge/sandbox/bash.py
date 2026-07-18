"""Docker-contained bash execution for the run_bash seed tool.

A single ``python:3.12-slim`` container is started eagerly at REPL boot (via
``start()``; a lock-guarded on-demand fallback covers ``/reset`` and direct use)
and lives for the sandbox object's lifetime (the REPL process). Each command
runs via ``docker exec bash -o pipefail -lc`` — a *fresh* shell per call, so
``cd``/env do not persist, with ``pipefail`` so a failure anywhere in a pipeline
reaches the exit code; the host ``./workspace`` dir is mounted read-write at ``/workspace``
(the working directory) so artifacts survive and are inspectable, and the repo
itself is never mounted. Docker is driven through the CLI via ``subprocess`` (no
docker SDK dependency); the subprocess call is injectable for unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from toolforge.config import SandboxSettings

# A subprocess runner: run argv (optionally time-limited), return (exit_code, combined output).
SubprocessRunner = Callable[..., Awaitable[tuple[int | None, bytes]]]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def truncate_output(text: str, cap: int) -> tuple[str, bool]:
    """Cap *text* to *cap* chars, keeping head+tail with a steering middle notice."""
    if len(text) <= cap:
        return text, False
    half = cap // 2
    notice = (
        f"\n\n[... output truncated: {len(text)} chars total; "
        f"showing the first and last {half}. Narrow the command's output "
        f"(grep/head/tail) to see more. ...]\n\n"
    )
    return text[:half] + notice + text[-half:], True


@dataclass
class BashResult:
    stdout: str
    exit_code: int | None
    timed_out: bool = False
    truncated: bool = False


async def _default_runner(argv: list[str], *, timeout: int | None) -> tuple[int | None, bytes]:
    """Run *argv*, merging stderr into stdout; kill the child on timeout/cancel."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        raise


class BashSandbox:
    """Manages one Docker container for run_bash (eager start, lazy fallback)."""

    def __init__(
        self, settings: SandboxSettings, *, runner: SubprocessRunner | None = None
    ) -> None:
        self._settings = settings
        self._runner = runner if runner is not None else _default_runner
        self._container_name = f"toolforge-sbx-{secrets.token_hex(4)}"
        self._started = False
        self._start_lock = asyncio.Lock()

    @property
    def container_name(self) -> str:
        return self._container_name

    @property
    def network_enabled(self) -> bool:
        """Whether the container can reach the network.

        Determines the trust level of this sandbox's output: with the network up,
        any command can pull external text (``curl``, ``pip``) into its stdout, so
        results must be quarantined as UNVERIFIED. See ``run_bash.py``.
        """
        return self._settings.network == "on"

    def _run_argv(self) -> list[str]:
        argv = ["docker", "run", "-d", "--name", self._container_name]
        if self._settings.network == "none":
            argv += ["--network", "none"]
        argv += [
            "-v",
            f"{self._settings.workspace_path}:/workspace",
            "-w",
            "/workspace",
            self._settings.image,
            "sleep",
            "infinity",
        ]
        return argv

    def _exec_argv(self, command: str) -> list[str]:
        # pipefail: without it a pipeline reports the LAST command's exit code,
        # so `curl … | head` exits 0 even when curl fails and the failure never
        # reaches is_error. (`;`-list masking remains; see run_bash description.)
        return [
            "docker",
            "exec",
            self._container_name,
            "bash",
            "-o",
            "pipefail",
            "-lc",
            command,
        ]

    async def start(self) -> None:
        """Start the container if not already running. Idempotent, concurrency-safe.

        The lock makes the check-then-``docker run`` atomic across the awaits, so
        concurrent cold callers can never race to create the same container name.
        """
        async with self._start_lock:
            if self._started:
                return
            self._settings.workspace_path.mkdir(parents=True, exist_ok=True)
            exit_code, out = await self._runner(self._run_argv(), timeout=60)
            if exit_code != 0:
                raise RuntimeError(
                    f"failed to start sandbox container: {strip_ansi(out.decode(errors='replace'))}"
                )
            self._started = True

    async def _ensure_started(self) -> None:
        if self._started:
            return
        await self.start()

    async def run(self, command: str, *, timeout: int | None = None) -> BashResult:
        """Run *command* in the container and return its captured output."""
        await self._ensure_started()
        effective_timeout = timeout if timeout is not None else self._settings.command_timeout
        try:
            exit_code, out = await self._runner(self._exec_argv(command), timeout=effective_timeout)
        except TimeoutError:
            return BashResult(
                stdout=f"[command timed out after {effective_timeout}s and was killed]",
                exit_code=None,
                timed_out=True,
            )
        text = strip_ansi(out.decode(errors="replace"))
        text, truncated = truncate_output(text, self._settings.output_cap)
        return BashResult(stdout=text, exit_code=exit_code, truncated=truncated)

    def teardown(self) -> None:
        """Force-remove the container. Idempotent; best-effort (errors swallowed).

        Synchronous so it can run from ``atexit`` after the event loop is gone.
        """
        if not self._started:
            return
        self._started = False
        with contextlib.suppress(Exception):
            import subprocess

            subprocess.run(
                ["docker", "rm", "-f", self._container_name],
                capture_output=True,
                timeout=30,
                check=False,
            )
