"""Settings tests for WorkerSettings and the cross-model separation check."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from toolforge.config import (
    AnthropicSettings,
    TestAuthorSettings,
    WorkerSettings,
    validate_worker_separation,
)


def test_worker_defaults(clean_provider_env: None) -> None:
    s = WorkerSettings()
    assert s.backend == "api"
    assert s.api_model == "claude-haiku-4-5"
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.model == "Qwen/Qwen3.6-27B"
    assert s.max_attempts == 4
    assert s.max_iterations == 15
    assert s.max_tokens == 8_000
    assert s.timeout_seconds == 1800


def test_worker_env_vars(clean_provider_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_WORKER_BACKEND", "local")
    monkeypatch.setenv("TOOLFORGE_WORKER_API_MODEL", "claude-haiku-99")
    monkeypatch.setenv("TOOLFORGE_WORKER_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("TOOLFORGE_WORKER_MAX_ITERATIONS", "5")
    monkeypatch.setenv("TOOLFORGE_WORKER_MAX_TOKENS", "4000")
    monkeypatch.setenv("TOOLFORGE_WORKER_TIMEOUT_SECONDS", "600")

    s = WorkerSettings()
    assert s.backend == "local"
    assert s.api_model == "claude-haiku-99"
    assert s.max_attempts == 2
    assert s.max_iterations == 5
    assert s.max_tokens == 4000
    assert s.timeout_seconds == 600


@pytest.mark.parametrize(
    "field", ["max_attempts", "max_iterations", "max_tokens", "timeout_seconds"]
)
def test_worker_rejects_non_positive(
    clean_provider_env: None, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv(f"TOOLFORGE_WORKER_{field.upper()}", "0")
    with pytest.raises(ValidationError, match="must be > 0"):
        WorkerSettings()


def test_effective_model_by_backend(clean_provider_env: None) -> None:
    api = WorkerSettings(_env_file=None, backend="api", api_model="m-api", model="m-local")
    local = WorkerSettings(_env_file=None, backend="local", api_model="m-api", model="m-local")
    assert api.effective_model == "m-api"
    assert local.effective_model == "m-local"


# ── validate_worker_separation ───────────────────────────────────────────────


def _anthropic(model: str, tmp_path: Path) -> AnthropicSettings:
    return AnthropicSettings(
        _env_file=None,
        auth_mode="api_key",
        api_key=SecretStr("test-key"),
        oauth_credentials_path=tmp_path / "unused.json",
        model=model,
    )


def test_separation_passes_when_distinct(clean_provider_env: None, tmp_path: Path) -> None:
    validate_worker_separation(
        WorkerSettings(_env_file=None, backend="api", api_model="claude-haiku-4-5"),
        _anthropic("claude-opus-4-8", tmp_path),
        TestAuthorSettings(_env_file=None, model=None),
    )


def test_separation_rejects_orchestrator_collision(
    clean_provider_env: None, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="orchestrator model"):
        validate_worker_separation(
            WorkerSettings(_env_file=None, backend="api", api_model="claude-opus-4-8"),
            _anthropic("claude-opus-4-8", tmp_path),
            TestAuthorSettings(_env_file=None, model=None),
        )


def test_separation_rejects_test_author_collision(clean_provider_env: None, tmp_path: Path) -> None:
    # The author's None model falls back to the orchestrator's — a worker that
    # collides with an explicit author override must also be rejected.
    with pytest.raises(ValueError, match="test-author model"):
        validate_worker_separation(
            WorkerSettings(_env_file=None, backend="local", model="some-model"),
            _anthropic("claude-opus-4-8", tmp_path),
            TestAuthorSettings(_env_file=None, model="some-model"),
        )
