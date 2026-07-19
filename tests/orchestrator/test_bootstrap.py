"""Bootstrap tests — host assembly, headless ask_user contract, injected hooks.

``build_host`` performs no network or container I/O, so it runs fine with fake
credentials and tmp paths; only the settings objects are real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from toolforge.config import (
    AnthropicSettings,
    OrchestratorSettings,
    SandboxSettings,
    TestAuthorSettings,
    WorkerSettings,
)
from toolforge.orchestrator.ask_user import AskUserRequest
from toolforge.orchestrator.bootstrap import build_host
from toolforge.orchestrator.hooks import HookEvent, HookManager


def _settings(
    tmp_path: Path,
) -> tuple[
    AnthropicSettings, OrchestratorSettings, SandboxSettings, WorkerSettings, TestAuthorSettings
]:
    return (
        AnthropicSettings(api_key="test-key", model="claude-opus-4-8"),
        OrchestratorSettings(runs_dir=tmp_path / "runs"),
        SandboxSettings(workspace_path=tmp_path / "workspace", tools_path=tmp_path / "tools"),
        WorkerSettings(backend="api", api_model="claude-haiku-4-5"),
        TestAuthorSettings(),
    )


async def _echo_ask(request: AskUserRequest) -> str:
    return request.options[0].label


def test_headless_omits_ask_user(tmp_path: Path) -> None:
    host = build_host(*_settings(tmp_path), ask_user=None)
    assert "ask_user" not in {s["name"] for s in host.registry.get_schemas()}


def test_ask_user_registered_when_callback_given(tmp_path: Path) -> None:
    host = build_host(*_settings(tmp_path), ask_user=_echo_ask)
    assert "ask_user" in {s["name"] for s in host.registry.get_schemas()}


def test_seed_tools_registered(tmp_path: Path) -> None:
    host = build_host(*_settings(tmp_path))
    names = {s["name"] for s in host.registry.get_schemas()}
    assert {"run_bash", "forge_tool", "register_tool"} <= names


def test_injected_hooks_are_used(tmp_path: Path) -> None:
    hooks = HookManager()
    hooks.register(HookEvent.ON_RESPONSE, lambda **kw: None)
    host = build_host(*_settings(tmp_path), hooks=hooks)
    assert host.hooks is hooks


def test_boot_findings_returned_not_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "broken").mkdir()  # tool dir with no manifest → warning
    host = build_host(*_settings(tmp_path))
    assert host.loaded_tools == []
    assert host.tool_store_warnings  # the corrupt dir surfaced as a warning
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_cross_model_violation_fails_at_boot(tmp_path: Path) -> None:
    settings = list(_settings(tmp_path))
    settings[3] = WorkerSettings(backend="api", api_model="claude-opus-4-8")
    with pytest.raises(ValueError, match="orchestrator model"):
        build_host(*settings)  # type: ignore[arg-type]
