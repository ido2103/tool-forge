"""Promotion and boot-loading of forged tools — the durable half of the forge.

``promote_candidate`` moves a verified candidate's artifacts out of the
agent-writable ``/workspace`` into the read-only tool store
(``<tools_path>/<name>/``) and writes the manifest; ``load_persisted_tools``
rebuilds the live registry from that store at boot. Promotion runs host-side
(harness code), which is exactly why the store can be agent-immutable: the
model can only reach it through ``register_tool``'s checks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from shutil import copyfile

from toolforge.forge.candidates import Candidate
from toolforge.forge.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    load_manifest,
    write_manifest,
)
from toolforge.forge.runtime import RUNNER_FILENAME, build_forged_tool
from toolforge.registry import ToolRegistry
from toolforge.sandbox import BashSandbox

_CONTAINER_WORKSPACE = "/workspace"


class PromotionError(Exception):
    """Promotion refused; the message says why and what to do instead."""


def _host_artifact(container_path: str, workspace_path: Path, what: str) -> Path:
    """Map a container ``/workspace/...`` path to its host file, safely.

    Rejects paths outside the workspace (including symlink escapes — the agent
    controls the workspace contents, so a candidate path must never be able to
    read arbitrary host files into the tool store).
    """
    if container_path != _CONTAINER_WORKSPACE and not container_path.startswith(
        _CONTAINER_WORKSPACE + "/"
    ):
        raise PromotionError(f"candidate {what} path {container_path!r} is not under /workspace")
    relative = container_path[len(_CONTAINER_WORKSPACE) :].lstrip("/")
    host = (workspace_path / relative).resolve()
    if not host.is_relative_to(workspace_path.resolve()):
        raise PromotionError(f"candidate {what} path {container_path!r} escapes the workspace")
    if not host.is_file():
        raise PromotionError(
            f"candidate {what} file {container_path!r} is missing from the workspace "
            "(was it deleted?); re-forge to rebuild it"
        )
    return host


def promote_candidate(
    candidate: Candidate,
    *,
    holdout_evidence: str,
    workspace_path: Path,
    tools_path: Path,
) -> Manifest:
    """Copy a candidate's artifacts into the tool store and write its manifest.

    Validate-everything-then-write: no file is created until every check has
    passed, so a failed promotion leaves the store untouched (and the caller
    keeps the candidate for a retry). Raises :class:`PromotionError`.
    """
    if candidate.code_path is None:
        raise PromotionError(
            f"candidate {candidate.name!r} has no code artifact; re-forge to rebuild it"
        )
    code_host = _host_artifact(candidate.code_path, workspace_path, "code")
    test_host = (
        _host_artifact(candidate.test_path, workspace_path, "test")
        if candidate.test_path is not None
        else None
    )
    tool_dir = tools_path / candidate.name
    if tool_dir.exists():
        raise PromotionError(
            f"tool store directory {candidate.name!r} already exists; the store is "
            "never overwritten — pick a different name"
        )

    manifest = Manifest(
        name=candidate.name,
        description=candidate.description,
        input_schema=candidate.input_schema,
        behavior=candidate.behavior,
        gap_analysis=candidate.gap_analysis,
        holdout_evidence=holdout_evidence,
        created_at=datetime.now(tz=UTC).isoformat(),
        allowed_domains=list(candidate.allowed_domains),
        examples=list(candidate.examples),
        test_report=candidate.test_report,
    )
    tool_dir.mkdir(parents=True)
    copyfile(code_host, tool_dir / "tool.py")
    if test_host is not None:
        copyfile(test_host, tool_dir / "test_tool.py")
    write_manifest(manifest, tool_dir)
    return manifest


def load_persisted_tools(
    tools_path: Path, sandbox: BashSandbox, registry: ToolRegistry
) -> tuple[list[str], list[str]]:
    """Register every valid persisted tool; returns ``(loaded_names, warnings)``.

    One corrupt directory must never brick the boot: anything unloadable —
    malformed manifest, missing tool.py, name collision with an already
    registered tool — becomes a warning and is skipped. Non-directories (the
    installed ``_runner.py``) are ignored silently.
    """
    loaded: list[str] = []
    warnings: list[str] = []
    if not tools_path.is_dir():
        return loaded, warnings
    for tool_dir in sorted(p for p in tools_path.iterdir() if p.is_dir()):
        try:
            manifest = load_manifest(tool_dir)
        except ManifestError as exc:
            warnings.append(f"skipping {tool_dir.name!r}: {exc}")
            continue
        try:
            registry.register(build_forged_tool(manifest, sandbox))
        except ValueError:
            warnings.append(
                f"skipping {tool_dir.name!r}: a tool with that name is already registered"
            )
            continue
        loaded.append(manifest.name)
    return loaded, warnings


__all__ = [
    "MANIFEST_FILENAME",
    "RUNNER_FILENAME",
    "PromotionError",
    "load_persisted_tools",
    "promote_candidate",
]
