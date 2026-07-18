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
    cache_ttl: Literal["5m", "1h"] = "5m"
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


class OrchestratorSettings(BaseSettings):
    """Agent-loop knobs: turn/token budget, prompt override, transcript sink."""

    model_config = SettingsConfigDict(
        env_prefix="TOOLFORGE_ORCHESTRATOR_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    max_tokens_per_turn: int = 32_000
    max_iterations: int = 30
    # None → the loop loads the bundled default prompt (orchestrator/prompts/system.md).
    system_prompt_path: Path | None = None
    runs_dir: Path = Path("runs")

    @field_validator("max_tokens_per_turn", "max_iterations")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be > 0")
        return v

    @field_validator("system_prompt_path", "runs_dir")
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None


class SandboxSettings(BaseSettings):
    """Docker-contained execution for the run_bash seed tool.

    ``network="on"`` keeps the default bridge network so pip/curl work in demos;
    ``"none"`` matches the spec's no-network-by-default posture for generated code.
    """

    model_config = SettingsConfigDict(
        env_prefix="TOOLFORGE_SANDBOX_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    image: str = "python:3.12-slim"
    network: Literal["on", "none"] = "on"
    workspace_path: Path = Path("./workspace")
    command_timeout: int = 60
    output_cap: int = 100_000

    @field_validator("command_timeout")
    @classmethod
    def _timeout_range(cls, v: int) -> int:
        if not 1 <= v <= 600:
            raise ValueError("command_timeout must be between 1 and 600 seconds")
        return v

    @field_validator("output_cap")
    @classmethod
    def _cap_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("output_cap must be > 0")
        return v

    @field_validator("workspace_path")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v.expanduser().resolve()
