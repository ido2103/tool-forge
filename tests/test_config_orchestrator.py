"""Settings tests for OrchestratorSettings and SandboxSettings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from toolforge.config import OrchestratorSettings, SandboxSettings


def test_orchestrator_defaults(clean_provider_env: None) -> None:
    s = OrchestratorSettings()
    assert s.max_tokens_per_turn == 32_000
    assert s.max_iterations == 30
    assert s.system_prompt_path is None
    assert s.runs_dir == Path("runs")


def test_orchestrator_env_vars(clean_provider_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_ORCHESTRATOR_MAX_TOKENS_PER_TURN", "8000")
    monkeypatch.setenv("TOOLFORGE_ORCHESTRATOR_MAX_ITERATIONS", "12")
    monkeypatch.setenv("TOOLFORGE_ORCHESTRATOR_SYSTEM_PROMPT_PATH", "~/prompts/sys.md")
    monkeypatch.setenv("TOOLFORGE_ORCHESTRATOR_RUNS_DIR", "/var/runs")

    s = OrchestratorSettings()
    assert s.max_tokens_per_turn == 8000
    assert s.max_iterations == 12
    assert s.system_prompt_path is not None
    assert not str(s.system_prompt_path).startswith("~")  # expanded
    assert s.runs_dir == Path("/var/runs")


def test_orchestrator_rejects_non_positive_tokens(clean_provider_env: None) -> None:
    with pytest.raises(ValidationError, match="must be > 0"):
        OrchestratorSettings(max_tokens_per_turn=0)


def test_orchestrator_rejects_non_positive_iterations(clean_provider_env: None) -> None:
    with pytest.raises(ValidationError, match="must be > 0"):
        OrchestratorSettings(max_iterations=0)


def test_sandbox_defaults(clean_provider_env: None) -> None:
    s = SandboxSettings()
    assert s.image == "python:3.12-slim"
    assert s.network == "on"
    assert s.command_timeout == 60
    assert s.output_cap == 100_000
    assert s.workspace_path.is_absolute()  # resolved


def test_sandbox_env_vars(clean_provider_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_SANDBOX_IMAGE", "python:3.13-slim")
    monkeypatch.setenv("TOOLFORGE_SANDBOX_NETWORK", "none")
    monkeypatch.setenv("TOOLFORGE_SANDBOX_COMMAND_TIMEOUT", "120")
    monkeypatch.setenv("TOOLFORGE_SANDBOX_OUTPUT_CAP", "5000")

    s = SandboxSettings()
    assert s.image == "python:3.13-slim"
    assert s.network == "none"
    assert s.command_timeout == 120
    assert s.output_cap == 5000


@pytest.mark.parametrize("bad", [0, 601, -1])
def test_sandbox_rejects_out_of_range_timeout(clean_provider_env: None, bad: int) -> None:
    with pytest.raises(ValidationError, match="between 1 and 600"):
        SandboxSettings(command_timeout=bad)


def test_sandbox_rejects_non_positive_cap(clean_provider_env: None) -> None:
    with pytest.raises(ValidationError, match="output_cap must be > 0"):
        SandboxSettings(output_cap=0)


def test_sandbox_rejects_bad_network(clean_provider_env: None) -> None:
    with pytest.raises(ValidationError):
        SandboxSettings(network="bridge")


def test_sandbox_workspace_resolved(clean_provider_env: None) -> None:
    s = SandboxSettings(workspace_path=Path("./relative/ws"))
    assert s.workspace_path.is_absolute()
