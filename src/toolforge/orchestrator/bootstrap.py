"""Host assembly — build the full orchestrator system once, for any host.

Every interactive or headless surface (the stdlib REPL, the Textual TUI, eval
harnesses, a future web/MCP host) boots the same way: validate the cross-model
invariant, wire provider clients, sandbox, forge pipeline, registry, and the
agent loop. :func:`build_host` is that single assembly point. Hosts differ only
in what they *inject* — a :class:`~toolforge.orchestrator.hooks.HookManager`
pre-loaded with their observers, and an ``ask_user`` callback (``None`` means
headless: the tool is never registered and the model never sees its schema).

The function performs no I/O beyond disk reads of the persisted toolbox — no
container start, no printing. Warnings and the loaded-tool list are returned on
the :class:`Host` for the caller to render however it likes.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass

from toolforge.config import (
    AnthropicSettings,
    LocalEndpointSettings,
    OrchestratorSettings,
    SandboxSettings,
    TestAuthorSettings,
    WorkerSettings,
    validate_worker_separation,
)
from toolforge.forge import (
    CandidateStore,
    ForgeWorker,
    TestAuthor,
    build_forge_tool,
    build_register_tool,
    install_runner,
    load_persisted_tools,
)
from toolforge.orchestrator.ask_user import AskUserCallback, build_ask_user
from toolforge.orchestrator.hooks import HookManager
from toolforge.orchestrator.loop import Orchestrator
from toolforge.orchestrator.prompts import load_system_prompt
from toolforge.orchestrator.transcript import Transcript, new_run_path
from toolforge.providers import AnthropicClient, OpenAICompatClient, ProviderClient
from toolforge.registry import ToolContext, ToolRegistry
from toolforge.sandbox import BashSandbox, build_run_bash


@dataclass
class Host:
    """Everything a host needs to run turns, plus boot-time findings to render."""

    orchestrator: Orchestrator
    sandbox: BashSandbox
    candidates: CandidateStore
    registry: ToolRegistry
    hooks: HookManager
    system_prompt: str
    model: str  # the orchestrator's effective model id, for status displays
    loaded_tools: list[str]
    tool_store_warnings: list[str]
    config_warnings: list[str]  # boot-time config findings (e.g. relaxed invariant)


def build_host(
    anthropic: AnthropicSettings,
    orch_settings: OrchestratorSettings,
    sandbox_settings: SandboxSettings,
    worker_settings: WorkerSettings,
    test_author_settings: TestAuthorSettings,
    *,
    hooks: HookManager | None = None,
    ask_user: AskUserCallback | None = None,
) -> Host:
    """Assemble the system. The caller still owns ``sandbox.start()``.

    ``hooks``: pass a manager with the host's observers already registered so
    they see every event from the first turn; ``None`` builds an empty one.
    ``ask_user``: the host's answer channel; ``None`` is the headless contract —
    the tool is not registered, so its schema never reaches the model.
    """
    # Fail loudly at boot on a cross-model violation, before any task runs
    # (local-vs-local collisions come back as warnings for the host to render).
    config_warnings = validate_worker_separation(
        worker_settings, anthropic, test_author_settings, orch_settings
    )

    # Build the shared Anthropic client iff any role runs the api backend — the
    # model is a per-send argument, so all api roles reuse one client and one
    # credentials path. Fully-local boots never touch Anthropic credentials
    # (AnthropicClient.__init__ enforces them).
    backends = (orch_settings.backend, worker_settings.backend, test_author_settings.backend)
    anthropic_client = AnthropicClient(anthropic) if "api" in backends else None

    def _client_for(backend: str, local: LocalEndpointSettings) -> ProviderClient:
        if backend == "api":
            assert anthropic_client is not None  # guaranteed by the guard above
            return anthropic_client
        return OpenAICompatClient(local)

    orch_client = _client_for(orch_settings.backend, orch_settings)
    orch_model = orch_settings.effective_model(anthropic.model)

    sandbox = BashSandbox(sandbox_settings)
    atexit.register(sandbox.teardown)

    if hooks is None:
        hooks = HookManager()

    worker_client = _client_for(worker_settings.backend, worker_settings)
    test_author = TestAuthor(
        _client_for(test_author_settings.backend, test_author_settings),
        sandbox,
        sandbox_settings,
        test_author_settings,
        model=test_author_settings.effective_model(anthropic.model),
    )
    # Sharing the host HookManager narrates the build live through the same
    # pre/post tool events the orchestrator's own calls fire.
    worker = ForgeWorker(
        worker_client,
        sandbox,
        sandbox_settings,
        worker_settings,
        model=worker_settings.effective_model,
        hooks=hooks,
        runs_dir=orch_settings.runs_dir,
    )

    registry = ToolRegistry(ToolContext())
    registry.register(build_run_bash(sandbox))
    if ask_user is not None:
        registry.register(build_ask_user(ask_user))
    candidates = CandidateStore()
    registry.register(
        build_forge_tool(candidates, registry, test_author=test_author, worker=worker, hooks=hooks)
    )
    registry.register(build_register_tool(candidates, registry, sandbox, sandbox_settings))

    # Reload the persisted toolbox: tools forged in earlier sessions come back
    # as live UNVERIFIED tools. A corrupt tool dir is skipped, never fatal.
    install_runner(sandbox_settings.tools_path)
    loaded, warnings = load_persisted_tools(sandbox_settings.tools_path, sandbox, registry)

    transcript = Transcript(new_run_path(orch_settings.runs_dir))
    system_prompt = load_system_prompt(orch_settings.system_prompt_path)

    orchestrator = Orchestrator(
        client=orch_client,
        registry=registry,
        hooks=hooks,
        model=orch_model,
        max_tokens=orch_settings.max_tokens_per_turn,
        max_iterations=orch_settings.max_iterations,
        transcript=transcript,
    )
    return Host(
        orchestrator=orchestrator,
        sandbox=sandbox,
        candidates=candidates,
        registry=registry,
        hooks=hooks,
        system_prompt=system_prompt,
        model=orch_model,
        loaded_tools=loaded,
        tool_store_warnings=warnings,
        config_warnings=config_warnings,
    )
