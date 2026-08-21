"""Settings tests for TestAuthorSettings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from toolforge.config import TestAuthorSettings


def test_test_author_defaults(clean_provider_env: None) -> None:
    s = TestAuthorSettings()
    assert s.backend == "api"
    assert s.model is None  # None → caller falls back to the orchestrator model
    assert s.local_model == "Qwen/Qwen3.6-27B"
    assert s.max_attempts == 3
    assert s.max_tokens == 16_000
    assert s.min_tests == 5
    assert s.timeout_seconds == 1500


def test_test_author_effective_model(clean_provider_env: None) -> None:
    api_default = TestAuthorSettings(_env_file=None, backend="api", model=None)
    api_override = TestAuthorSettings(_env_file=None, backend="api", model="claude-y")
    local = TestAuthorSettings(_env_file=None, backend="local", local_model="qwen-27b")
    assert api_default.effective_model("claude-x") == "claude-x"
    assert api_override.effective_model("claude-x") == "claude-y"
    assert local.effective_model("claude-x") == "qwen-27b"


def test_test_author_backend_env_vars(
    clean_provider_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_BACKEND", "local")
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_LOCAL_MODEL", "qwen-27b")
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_HOST", "192.168.1.250")
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_PORT", "8090")

    s = TestAuthorSettings()
    assert s.backend == "local"
    assert s.local_model == "qwen-27b"
    assert s.base_url == "http://192.168.1.250:8090/v1"


def test_test_author_env_vars(clean_provider_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_MAX_TOKENS", "8000")
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_MIN_TESTS", "3")
    monkeypatch.setenv("TOOLFORGE_TEST_AUTHOR_TIMEOUT_SECONDS", "600")

    s = TestAuthorSettings()
    assert s.model == "claude-sonnet-5"
    assert s.max_attempts == 2
    assert s.max_tokens == 8000
    assert s.min_tests == 3
    assert s.timeout_seconds == 600


@pytest.mark.parametrize("field", ["max_attempts", "max_tokens", "min_tests", "timeout_seconds"])
def test_test_author_rejects_non_positive(
    clean_provider_env: None, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv(f"TOOLFORGE_TEST_AUTHOR_{field.upper()}", "0")
    with pytest.raises(ValidationError, match="must be > 0"):
        TestAuthorSettings()
