"""Sandbox: where model-driven code runs.

v0 provides the Docker-contained ``run_bash`` seed tool: a lazily-started
``python:3.12-slim`` container with the host ``./workspace`` mounted at
``/workspace``, network on by default (config-toggleable), driven through the
docker CLI. The spec's per-tool domain allowlists, no-network-default for
*generated* code, and credential-access logging are future slices.
"""

from toolforge.sandbox.bash import BashResult, BashSandbox, strip_ansi, truncate_output
from toolforge.sandbox.run_bash import build_run_bash

__all__ = [
    "BashResult",
    "BashSandbox",
    "build_run_bash",
    "strip_ansi",
    "truncate_output",
]
