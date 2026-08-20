"""Runtime configuration — pydantic-settings models reading ``.env`` + environment.

One settings class per model role. Precedence (highest wins): init kwargs >
``os.environ`` > ``.env``. See ``.env.example`` at the repo root for every
variable and its default. No YAML layer — add one only if config outgrows
``.env``.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
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

    @property
    def has_credentials(self) -> bool:
        """True when the configured auth mode has usable credentials."""
        if self.auth_mode == "api_key":
            return self.api_key is not None
        return self.oauth_credentials_path.exists()

    def require_credentials(self) -> None:
        """Raise ``ValueError`` when credentials are absent.

        Called by ``AnthropicClient`` at construction — settings themselves must
        build credential-free so a fully-local boot needs no Anthropic account.
        """
        if self.auth_mode == "api_key" and self.api_key is None:
            raise ValueError("ANTHROPIC_API_KEY is required when auth_mode='api_key'")
        if self.auth_mode == "oauth" and not self.oauth_credentials_path.exists():
            raise ValueError(
                f"OAuth credentials file not found: {self.oauth_credentials_path} "
                "(required when auth_mode='oauth')"
            )


class LocalEndpointSettings(BaseSettings):
    """Shared fields for a local OpenAI-compatible endpoint (vLLM / llama.cpp / LM Studio).

    Base class only — always instantiate a role subclass, whose ``model_config``
    supplies the env prefix (pydantic-settings applies it to inherited fields).
    """

    host: str = "127.0.0.1"
    port: int = 8000
    # vLLM convention: server without --api-key accepts any value, but the
    # OpenAI SDK requires a non-empty key.
    api_key: SecretStr = SecretStr("EMPTY")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"


class WorkerSettings(LocalEndpointSettings):
    """Forge-worker backend selection, model access, and build-loop budgets.

    Two first-class backends: ``api`` (default; a cheaper Anthropic model
    reusing the orchestrator's credentials — the model is a per-send argument
    on the client, so no second auth path) and ``local`` (any OpenAI-compatible
    server: vLLM / llama.cpp / LM Studio). The cross-model invariant (worker ≠
    orchestrator / test author) is checked at boot by
    :func:`validate_worker_separation`, not here — this class cannot see the
    other settings.
    """

    model_config = SettingsConfigDict(
        env_prefix="TOOLFORGE_WORKER_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    backend: Literal["api", "local"] = "api"
    api_model: str = "claude-haiku-4-5"
    # local mode: OpenAI-compatible server (endpoint fields — host/port/api_key —
    # inherited from LocalEndpointSettings under the TOOLFORGE_WORKER_ prefix).
    model: str = "Qwen/Qwen3.6-27B"
    # Build-loop budgets — bounded in code, never tool parameters (a failed
    # forge is answered with a better spec, not a bigger budget).
    max_attempts: int = 4  # authoritative harness verifications per build
    max_iterations: int = 15  # tool-call iterations per worker attempt
    max_tokens: int = 8_000  # output-token cap per worker model call
    # Wall-clock ceiling for one build() call, checked before every worker run
    # and verification; overshoot is bounded by the longest single step.
    timeout_seconds: int = 1800

    @property
    def effective_model(self) -> str:
        """The model the worker actually runs, per the selected backend."""
        return self.api_model if self.backend == "api" else self.model

    @field_validator("max_attempts", "max_iterations", "max_tokens", "timeout_seconds")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be > 0")
        return v


class TestAuthorSettings(LocalEndpointSettings):
    """Forge test-author knobs: backend/model selection plus the authoring-loop budget.

    The test author is frontier-tier by design (it writes the adversarial tests
    the worker must satisfy), so the default backend is ``api`` with
    ``model=None`` — "use the orchestrator's api model", which preserves the
    author-vs-worker cross-model invariant without a second credentials path.
    A ``local`` backend (own endpoint fields under this prefix, model id in
    ``local_model``) is available for fully-local setups.
    """

    # The Test* name matches pytest's collection convention; opt out explicitly.
    __test__: ClassVar[bool] = False

    model_config = SettingsConfigDict(
        env_prefix="TOOLFORGE_TEST_AUTHOR_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    backend: Literal["api", "local"] = "api"
    # api mode: overrides the model id; None → the orchestrator's api model.
    model: str | None = None
    # local mode only — the model id served at the author's endpoint. Named
    # local_model (not model, as on WorkerSettings) to keep the long-standing
    # TOOLFORGE_TEST_AUTHOR_MODEL meaning intact.
    local_model: str = "Qwen/Qwen3.6-27B"
    max_attempts: int = 3
    max_tokens: int = 16_000
    min_tests: int = 5
    # Wall-clock ceiling for one author_tests() call: every model call and
    # sandbox command checks the deadline before starting, so overshoot is
    # bounded by the longest single step.
    timeout_seconds: int = 1500

    def effective_model(self, api_default: str) -> str:
        """The model the test author actually runs, per the selected backend."""
        if self.backend == "api":
            return self.model or api_default
        return self.local_model

    @field_validator("max_attempts", "max_tokens", "min_tests", "timeout_seconds")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be > 0")
        return v


class OrchestratorSettings(LocalEndpointSettings):
    """Orchestrator backend selection plus agent-loop knobs.

    Two backends, mirroring the worker: ``api`` (default; Anthropic, model id
    from ``TOOLFORGE_ANTHROPIC_MODEL``) and ``local`` (any OpenAI-compatible
    server; endpoint fields under this prefix, model id in ``model``). Loop
    knobs: turn/token budget, prompt override, transcript sink.
    """

    model_config = SettingsConfigDict(
        env_prefix="TOOLFORGE_ORCHESTRATOR_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    backend: Literal["api", "local"] = "api"
    # local mode only — the model id served at http://{host}:{port}/v1. In api
    # mode the orchestrator model comes from TOOLFORGE_ANTHROPIC_MODEL.
    model: str = "Qwen/Qwen3.6-27B"
    # Local servers often cap output tokens well below 32k — tune this down
    # when running backend=local.
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

    def effective_model(self, api_model: str) -> str:
        """The model the orchestrator actually runs: *api_model* (the caller
        passes ``AnthropicSettings.model``) in api mode, ``model`` otherwise."""
        return api_model if self.backend == "api" else self.model


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
    # Project-relative on purpose (like workspace_path/runs): a per-project
    # toolbox keeps eval runs isolated and grown tools visible in the repo.
    tools_path: Path = Path("./tools")
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

    @field_validator("workspace_path", "tools_path")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v.expanduser().resolve()


def validate_worker_separation(
    worker: WorkerSettings,
    anthropic: AnthropicSettings,
    test_author: TestAuthorSettings,
    orchestrator: OrchestratorSettings,
) -> list[str]:
    """Enforce the cross-model invariant at boot: worker ≠ orchestrator/test author.

    Cross-model separation mitigates the self-verification trap — the model
    that implements a tool must never be the one that wrote its tests or the
    one that judges the result. Raises ``ValueError`` so a misconfigured boot
    fails loudly before any task runs.

    One relaxation: when BOTH colliding roles run local backends the collision
    downgrades to a returned warning string (single-GPU convenience — one
    llama.cpp server, one model). Any collision involving an api-backend role
    still raises.
    """
    warnings: list[str] = []
    worker_model = worker.effective_model
    orch_model = orchestrator.effective_model(anthropic.model)
    author_model = test_author.effective_model(anthropic.model)

    if worker_model == orch_model:
        if worker.backend == "local" and orchestrator.backend == "local":
            warnings.append(
                f"worker and orchestrator share the local model {worker_model!r}; "
                "cross-model separation is degraded (self-verification risk)"
            )
        else:
            raise ValueError(
                f"worker model {worker_model!r} equals the orchestrator model; the forge "
                "worker must be a different model (set TOOLFORGE_WORKER_API_MODEL / "
                "TOOLFORGE_WORKER_MODEL or change the orchestrator model)"
            )
    if worker_model == author_model:
        if worker.backend == "local" and test_author.backend == "local":
            warnings.append(
                f"worker and test author share the local model {worker_model!r}; "
                "the tests and the implementation come from the same model"
            )
        else:
            raise ValueError(
                f"worker model {worker_model!r} equals the test-author model; the model "
                "that implements a tool must not be the one that wrote its tests"
            )
    return warnings
