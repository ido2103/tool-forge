"""Promotion + boot-loader tests — host-side file movement, no Docker."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from toolforge.config import SandboxSettings
from toolforge.forge.candidates import Candidate
from toolforge.forge.manifest import load_manifest
from toolforge.forge.promote import PromotionError, load_persisted_tools, promote_candidate
from toolforge.registry import ToolContext, ToolRegistry
from toolforge.sandbox.bash import BashSandbox

from tests.sandbox.test_bash import FakeRunner


def _candidate(workspace: Path, name: str = "fetch_rss", *, with_tests: bool = True) -> Candidate:
    build_dir = workspace / "build" / name
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "tool.py").write_text("def run(url):\n    return url\n")
    if with_tests:
        (build_dir / "test_tool.py").write_text("def test_ok():\n    pass\n")
    return Candidate(
        name=name,
        description="Fetch an RSS feed URL and return its entries as titled text.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The feed URL."}},
            "required": ["url"],
        },
        behavior="Returns one line per entry; on HTTP failure returns an error string.",
        gap_analysis="run_bash cannot parse RSS without per-call boilerplate scripts",
        allowed_domains=["feeds.example.com"],
        code_path=f"/workspace/build/{name}/tool.py",
        test_path=f"/workspace/build/{name}/test_tool.py" if with_tests else None,
        test_report="4 passed",
    )


def _promote(candidate: Candidate, tmp_path: Path) -> None:
    promote_candidate(
        candidate,
        holdout_evidence="ran 3 unseen feeds; outputs matched",
        workspace_path=tmp_path / "workspace",
        tools_path=tmp_path / "tools",
    )


# ── promote_candidate ────────────────────────────────────────────────────────


def test_happy_path_copies_artifacts_and_manifest(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "workspace")
    _promote(candidate, tmp_path)
    tool_dir = tmp_path / "tools" / "fetch_rss"
    assert (tool_dir / "tool.py").read_text() == "def run(url):\n    return url\n"
    assert (tool_dir / "test_tool.py").read_text() == "def test_ok():\n    pass\n"
    manifest = load_manifest(tool_dir)
    assert manifest.name == "fetch_rss"
    assert manifest.holdout_evidence == "ran 3 unseen feeds; outputs matched"
    assert manifest.allowed_domains == ["feeds.example.com"]
    assert manifest.test_report == "4 passed"
    assert manifest.created_at  # stamped


def test_testless_candidate_promotes_without_test_file(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "workspace", with_tests=False)
    _promote(candidate, tmp_path)
    tool_dir = tmp_path / "tools" / "fetch_rss"
    assert (tool_dir / "tool.py").is_file()
    assert not (tool_dir / "test_tool.py").exists()


def test_no_code_artifact_rejected(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "workspace")
    candidate.code_path = None
    with pytest.raises(PromotionError, match="no code artifact"):
        _promote(candidate, tmp_path)


def test_missing_code_file_rejected_and_store_untouched(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "workspace")
    (tmp_path / "workspace" / "build" / "fetch_rss" / "tool.py").unlink()
    with pytest.raises(PromotionError, match="missing from the workspace"):
        _promote(candidate, tmp_path)
    assert not (tmp_path / "tools" / "fetch_rss").exists()


@pytest.mark.parametrize(
    "escape",
    ["/etc/passwd", "/workspace/../etc/passwd", "relative/tool.py", "/workspaces/tool.py"],
)
def test_path_outside_workspace_rejected(tmp_path: Path, escape: str) -> None:
    candidate = _candidate(tmp_path / "workspace")
    candidate.code_path = escape
    with pytest.raises(PromotionError, match="not under /workspace|escapes the workspace"):
        _promote(candidate, tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_symlink_escape_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    candidate = _candidate(workspace)
    secret = tmp_path / "secret.py"
    secret.write_text("def run():\n    return 'stolen'\n")
    link = workspace / "build" / "fetch_rss" / "tool.py"
    link.unlink()
    link.symlink_to(secret)
    with pytest.raises(PromotionError, match="escapes the workspace"):
        _promote(candidate, tmp_path)


def test_failed_write_rolls_back_store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure mid-write must not leave a partial dir that blocks the name forever."""
    candidate = _candidate(tmp_path / "workspace")

    def explode(manifest: object, tool_dir: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("toolforge.forge.promote.write_manifest", explode)
    with pytest.raises(OSError, match="disk full"):
        _promote(candidate, tmp_path)
    assert not (tmp_path / "tools" / "fetch_rss").exists()

    # The name is still promotable once the fault clears.
    monkeypatch.undo()
    _promote(candidate, tmp_path)
    assert (tmp_path / "tools" / "fetch_rss" / "manifest.json").is_file()


def test_existing_store_dir_rejected(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path / "workspace")
    (tmp_path / "tools" / "fetch_rss").mkdir(parents=True)
    with pytest.raises(PromotionError, match="already exists"):
        _promote(candidate, tmp_path)


# ── load_persisted_tools ─────────────────────────────────────────────────────


def _loader_env(sandbox_settings: SandboxSettings) -> tuple[BashSandbox, ToolRegistry]:
    sandbox = BashSandbox(sandbox_settings, runner=FakeRunner([]))
    return sandbox, ToolRegistry(ToolContext())


def test_loader_registers_promoted_tool(tmp_path: Path, sandbox_settings: SandboxSettings) -> None:
    _promote(_candidate(tmp_path / "workspace"), tmp_path)
    sandbox, registry = _loader_env(sandbox_settings)
    loaded, warnings = load_persisted_tools(tmp_path / "tools", sandbox, registry)
    assert loaded == ["fetch_rss"]
    assert warnings == []
    assert registry.has("fetch_rss")
    assert registry.trust_for("fetch_rss") == "UNVERIFIED"
    assert registry.serial_group_for("fetch_rss") == "sandbox"


def test_loader_missing_dir_is_empty(sandbox_settings: SandboxSettings, tmp_path: Path) -> None:
    sandbox, registry = _loader_env(sandbox_settings)
    assert load_persisted_tools(tmp_path / "nope", sandbox, registry) == ([], [])


def test_loader_skips_corrupt_dir_and_loads_rest(
    tmp_path: Path, sandbox_settings: SandboxSettings
) -> None:
    _promote(_candidate(tmp_path / "workspace", name="good_tool"), tmp_path)
    corrupt = tmp_path / "tools" / "bad_tool"
    corrupt.mkdir(parents=True)
    (corrupt / "manifest.json").write_text("{broken")
    sandbox, registry = _loader_env(sandbox_settings)
    loaded, warnings = load_persisted_tools(tmp_path / "tools", sandbox, registry)
    assert loaded == ["good_tool"]
    assert len(warnings) == 1
    assert "bad_tool" in warnings[0]
    assert registry.has("good_tool")
    assert not registry.has("bad_tool")


def test_loader_skips_name_collision(tmp_path: Path, sandbox_settings: SandboxSettings) -> None:
    _promote(_candidate(tmp_path / "workspace"), tmp_path)
    sandbox, registry = _loader_env(sandbox_settings)
    load_persisted_tools(tmp_path / "tools", sandbox, registry)
    loaded, warnings = load_persisted_tools(tmp_path / "tools", sandbox, registry)
    assert loaded == []
    assert len(warnings) == 1
    assert "already registered" in warnings[0]


def test_loader_ignores_runner_file(tmp_path: Path, sandbox_settings: SandboxSettings) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "_runner.py").write_text("# runner\n")
    sandbox, registry = _loader_env(sandbox_settings)
    assert load_persisted_tools(tools, sandbox, registry) == ([], [])
