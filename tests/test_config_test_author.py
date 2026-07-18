"""Settings tests for TestAuthorSettings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from toolforge.config import TestAuthorSettings


def test_test_author_defaults(clean_provider_env: None) -> None:
    s = TestAuthorSettings()
    assert s.model is None  # None → caller falls back to the orchestrator model
    assert s.max_attempts == 3
    assert s.max_tokens == 16_000
    assert s.min_tests == 5
    assert s.timeout_seconds == 1500


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
