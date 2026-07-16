"""Settings tests — env var parsing, aliases, validators, secret hygiene."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from toolforge.config import AnthropicSettings, WorkerSettings


def test_anthropic_defaults(clean_provider_env: None) -> None:
    s = AnthropicSettings(api_key=SecretStr("k"))
    assert s.auth_mode == "api_key"
    assert s.model == "claude-opus-4-8"
    assert s.base_url is None
    assert s.cache_ttl == "ephemeral"
    assert s.extended_thinking == "adaptive"
    assert s.oauth_credentials_path.is_absolute()  # "~" expanded


def test_anthropic_env_vars(
    clean_provider_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"accessToken": "t", "refreshToken": "r", "expiresAt": 1}))
    monkeypatch.setenv("TOOLFORGE_ANTHROPIC_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOLFORGE_ANTHROPIC_OAUTH_CREDENTIALS_PATH", str(creds))
    monkeypatch.setenv("TOOLFORGE_ANTHROPIC_MODEL", "claude-other")
    monkeypatch.setenv("TOOLFORGE_ANTHROPIC_BASE_URL", "http://proxy:8080")
    monkeypatch.setenv("TOOLFORGE_ANTHROPIC_CACHE_TTL", "1h")
    monkeypatch.setenv("TOOLFORGE_ANTHROPIC_EXTENDED_THINKING", "off")

    s = AnthropicSettings()
    assert s.auth_mode == "oauth"
    assert s.oauth_credentials_path == creds
    assert s.model == "claude-other"
    assert s.base_url == "http://proxy:8080"
    assert s.cache_ttl == "1h"
    assert s.extended_thinking == "off"


def test_anthropic_api_key_standard_env_alias(
    clean_provider_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-standard-env")
    s = AnthropicSettings()
    assert s.api_key is not None
    assert s.api_key.get_secret_value() == "from-standard-env"


def test_anthropic_api_key_prefixed_env_alias(
    clean_provider_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOLFORGE_ANTHROPIC_API_KEY", "from-prefixed-env")
    s = AnthropicSettings()
    assert s.api_key is not None
    assert s.api_key.get_secret_value() == "from-prefixed-env"


def test_api_key_required_in_api_key_mode(clean_provider_env: None) -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY is required"):
        AnthropicSettings()


def test_oauth_mode_requires_existing_creds_file(clean_provider_env: None, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="credentials file not found"):
        AnthropicSettings(auth_mode="oauth", oauth_credentials_path=tmp_path / "missing.json")


def test_oauth_path_expanduser(clean_provider_env: None) -> None:
    s = AnthropicSettings(
        api_key=SecretStr("k"),
        oauth_credentials_path=Path("~/never/creds.json"),
    )
    assert not str(s.oauth_credentials_path).startswith("~")
    assert s.oauth_credentials_path.is_absolute()


def test_secret_values_not_leaked(clean_provider_env: None) -> None:
    s = AnthropicSettings(api_key=SecretStr("super-secret"))
    assert "super-secret" not in repr(s)
    assert "super-secret" not in str(s)


def test_worker_defaults_and_base_url(clean_provider_env: None) -> None:
    w = WorkerSettings()
    assert w.host == "127.0.0.1"
    assert w.port == 8000
    assert w.model == "Qwen/Qwen3.6-27B"
    assert w.api_key.get_secret_value() == "EMPTY"
    assert w.base_url == "http://127.0.0.1:8000/v1"


def test_worker_env_vars(clean_provider_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLFORGE_WORKER_HOST", "192.168.1.5")
    monkeypatch.setenv("TOOLFORGE_WORKER_PORT", "8080")
    monkeypatch.setenv("TOOLFORGE_WORKER_MODEL", "qwen-local")
    monkeypatch.setenv("TOOLFORGE_WORKER_API_KEY", "secret-worker-key")

    w = WorkerSettings()
    assert w.base_url == "http://192.168.1.5:8080/v1"
    assert w.model == "qwen-local"
    assert w.api_key.get_secret_value() == "secret-worker-key"
    assert "secret-worker-key" not in repr(w)
