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
    model: str  # the orchestrator's model id, for status displays
    loaded_tools: list[str]
    tool_store_warnings: list[str]


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
    # Fail loudly at boot on a cross-model violation, before any task runs.
    validate_worker_separation(worker_settings, anthropic, test_author_settings)

    client = AnthropicClient(anthropic)
    sandbox = BashSandbox(sandbox_settings)
    atexit.register(sandbox.teardown)

    if hooks is None:
        hooks = HookManager()

    # api mode reuses the orchestrator's client — the model is a per-send
    # argument, so no second credentials path; local mode gets its own client.
    worker_client: ProviderClient = (
        client if worker_settings.backend == "api" else OpenAICompatClient(worker_settings)
    )
    test_author = TestAuthor(
        client,
        sandbox,
        sandbox_settings,
        test_author_settings,
        model=test_author_settings.model or anthropic.model,
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
        client=client,
        registry=registry,
        hooks=hooks,
        model=anthropic.model,
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
        model=anthropic.model,
        loaded_tools=loaded,
        tool_store_warnings=warnings,
    )
