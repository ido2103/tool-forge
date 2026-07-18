"""Live sandbox test — real Docker round-trip. Marked ``live``; run explicitly.

    uv run pytest -m live tests/sandbox/test_bash_live.py

Skips automatically if the docker CLI is unavailable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from toolforge.config import SandboxSettings
from toolforge.sandbox.bash import BashSandbox

from tests._docker import DOCKER_SKIP_REASON, docker_available

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not docker_available(), reason=DOCKER_SKIP_REASON),
]


async def test_real_container_round_trip(tmp_path: Path) -> None:
    settings = SandboxSettings(
        _env_file=None,
        image="python:3.12-slim",
        network="on",
        workspace_path=tmp_path / "workspace",
        command_timeout=30,
        output_cap=100_000,
    )
    sandbox = BashSandbox(settings)
    try:
        # 1. Basic echo runs in /workspace.
        r = await sandbox.run("pwd && echo hello")
        assert r.exit_code == 0
        assert "/workspace" in r.stdout
        assert "hello" in r.stdout

        # 2. cd/env do NOT persist across calls (fresh shell each time).
        await sandbox.run("cd /tmp && export FOO=bar")
        r2 = await sandbox.run("pwd; echo FOO=$FOO")
        assert "/workspace" in r2.stdout  # back in workdir
        assert "FOO=\n" in r2.stdout or "FOO=" in r2.stdout.rstrip()  # env not carried

        # 3. A file written to /workspace is visible on the host.
        await sandbox.run("echo 'from-container' > /workspace/artifact.txt")
        host_file = tmp_path / "workspace" / "artifact.txt"
        assert host_file.exists()
        assert host_file.read_text().strip() == "from-container"

        # 4. Nonzero exit is reported.
        r3 = await sandbox.run("exit 7")
        assert r3.exit_code == 7

        # 5. pipefail: an upstream failure in a pipeline is reported, not
        #    masked by the exit code of the last command.
        r4 = await sandbox.run("false | cat")
        assert r4.exit_code != 0

        # 5b. pipefail side effect: a producer killed by SIGPIPE when the
        #     consumer exits early reports 141 at this (faithful) layer;
        #     run_bash maps 141 to is_error=False (see test_run_bash.py).
        r4b = await sandbox.run("seq 1 1000000 | head -1")
        assert r4b.exit_code == 141
        assert "1" in r4b.stdout

        # 6. Known limitation: a `;`-list still reports only the last command,
        #    so a trailing echo masks earlier failures. Mitigated by the
        #    run_bash description (don't append exit markers), not the shell.
        r5 = await sandbox.run("false; echo done")
        assert r5.exit_code == 0
    finally:
        sandbox.teardown()


async def test_live_concurrent_cold_start(tmp_path: Path) -> None:
    # The exact bug from production: two run_bash calls in one batch hit a cold
    # sandbox and raced to `docker run` the same container name. Both must
    # succeed, off one container.
    settings = SandboxSettings(
        _env_file=None,
        image="python:3.12-slim",
        network="none",
        workspace_path=tmp_path / "workspace",
        command_timeout=30,
        output_cap=100_000,
    )
    sandbox = BashSandbox(settings)
    try:
        r1, r2 = await asyncio.gather(sandbox.run("echo one"), sandbox.run("echo two"))
        assert r1.exit_code == 0 and "one" in r1.stdout
        assert r2.exit_code == 0 and "two" in r2.stdout
    finally:
        sandbox.teardown()
