"""Runtime configuration — pydantic-settings models reading ``.env`` + environment.

One settings class per model role. Precedence (highest wins): init kwargs >
``os.environ`` > ``.env``. See ``.env.example`` at the repo root for every
variable and its default. No YAML layer — add one only if config outgrows
``.env``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnthropicSettings(BaseSettings):
    """Orchestrator model access (Anthropic Messages API)."""

    model_config = SettingsConfigDict(
        env_prefix="TOOLFORGE_ANTHROPIC_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    auth_mode: Literal["api_key", "oauth"] = "api_key"
    api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "TOOLFORGE_ANTHROPIC_API_KEY"),
    )
    oauth_credentials_path: Path = Path("~/.config/toolforge/anthropic_oauth.json")
    model: str = "claude-opus-4-8"
    base_url: str | None = None
    cache_ttl: Literal["ephemeral", "1h"] = "ephemeral"
    extended_thinking: Literal["adaptive", "off"] = "adaptive"

    @field_validator("oauth_credentials_path")
    @classmethod
    def _expand_user(cls, v: Path) -> Path:
        return v.expanduser()

    @model_validator(mode="after")
    def _check_auth(self) -> Self:
        if self.auth_mode == "api_key" and self.api_key is None:
            raise ValueError("ANTHROPIC_API_KEY is required when auth_mode='api_key'")
        if self.auth_mode == "oauth" and not self.oauth_credentials_path.exists():
            raise ValueError(
                f"OAuth credentials file not found: {self.oauth_credentials_path} "
                "(required when auth_mode='oauth')"
            )
        return self


class WorkerSettings(BaseSettings):
    """Forge-worker model access (OpenAI-compatible server: vLLM / llama.cpp)."""

    model_config = SettingsConfigDict(
        env_prefix="TOOLFORGE_WORKER_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    host: str = "127.0.0.1"
    port: int = 8000
    model: str = "Qwen/Qwen3.6-27B"
    # vLLM convention: server without --api-key accepts any value, but the
    # OpenAI SDK requires a non-empty key.
    api_key: SecretStr = SecretStr("EMPTY")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"
