"""Docker availability probe for ``live``-marked sandbox tests.

``shutil.which("docker")`` is not sufficient: WSL keeps a ``docker`` shim on PATH
even when Docker Desktop integration is off, so the binary "exists" but every
command fails. Probing ``docker info`` checks the daemon is actually reachable,
which is what these tests need.
"""

from __future__ import annotations

import functools
import shutil
import subprocess


@functools.cache
def docker_available() -> bool:
    """True if the docker CLI exists AND its daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


DOCKER_SKIP_REASON = "docker daemon not available"
