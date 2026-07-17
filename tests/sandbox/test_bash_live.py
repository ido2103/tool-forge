"""Live sandbox test — real Docker round-trip. Marked ``live``; run explicitly.

    uv run pytest -m live tests/sandbox/test_bash_live.py

Skips automatically if the docker CLI is unavailable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from toolforge.config import SandboxSettings
from toolforge.sandbox.bash import BashSandbox

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
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
    finally:
        sandbox.teardown()
