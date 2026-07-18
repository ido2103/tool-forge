"""Minimal streaming REPL — talk to the orchestrator from a terminal.

``toolforge "a task"`` runs one task and exits; ``toolforge`` with no argument
opens an interactive multi-turn session. The sandbox container is started
eagerly at boot — a clear failure if Docker is down, instead of a mid-task
surprise. Thinking streams dimmed, answer text plain, and each tool call prints
a compact one-liner. Ctrl-C requests a graceful stop of the in-flight turn;
``/new`` clears history, ``/reset`` also recycles the container, ``/quit``
exits. Stdlib only — no rich/typer.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import signal
import sys
from typing import Any

from pydantic import ValidationError

from toolforge.config import AnthropicSettings, OrchestratorSettings, SandboxSettings
from toolforge.forge import CandidateStore, build_forge_tool, build_register_tool
from toolforge.orchestrator.hooks import HookEvent, HookManager
from toolforge.orchestrator.loop import Orchestrator
from toolforge.orchestrator.prompts import load_system_prompt
from toolforge.orchestrator.transcript import Transcript, new_run_path
from toolforge.providers import AnthropicClient, Message
from toolforge.registry import ToolContext, ToolRegistry
from toolforge.sandbox import BashSandbox, build_run_bash

_USE_COLOR = sys.stdout.isatty()


def _style(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _dim(text: str) -> str:
    return _style(text, "2")


def _cyan(text: str) -> str:
    return _style(text, "36")


def _install_tool_oneliners(hooks: HookManager) -> None:
    """Print a compact line per tool call via the loop's hooks."""

    def pre(**kw: Any) -> None:
        preview = ""
        inp = kw.get("input") or {}
        if isinstance(inp, dict):
            cmd = inp.get("command")
            if isinstance(cmd, str):
                preview = cmd if len(cmd) <= 80 else cmd[:77] + "…"
        sys.stdout.write(_cyan(f"\n→ {kw.get('tool_name')}: {preview}\n"))
        sys.stdout.flush()

    def post(**kw: Any) -> None:
        mark = "✗" if kw.get("is_error") else "✓"
        sys.stdout.write(_dim(f"  {mark} ({kw.get('latency_ms')}ms)\n"))
        sys.stdout.flush()

    hooks.register(HookEvent.ON_TOOL_PRE_EXECUTE, pre)
    hooks.register(HookEvent.ON_TOOL_POST_EXECUTE, post)


async def _on_thinking(text: str) -> None:
    sys.stdout.write(_dim(text))
    sys.stdout.flush()


async def _on_text(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _build(
    anthropic: AnthropicSettings,
    orch_settings: OrchestratorSettings,
    sandbox_settings: SandboxSettings,
) -> tuple[Orchestrator, BashSandbox, str]:
    client = AnthropicClient(anthropic)
    sandbox = BashSandbox(sandbox_settings)
    atexit.register(sandbox.teardown)

    registry = ToolRegistry(ToolContext())
    registry.register(build_run_bash(sandbox))
    candidates = CandidateStore()
    registry.register(build_forge_tool(candidates, registry))
    registry.register(build_register_tool(candidates, registry))

    hooks = HookManager()
    _install_tool_oneliners(hooks)

    transcript = Transcript(new_run_path(orch_settings.runs_dir))
    system_prompt = load_system_prompt(orch_settings.system_prompt_path)

    orch = Orchestrator(
        client=client,
        registry=registry,
        hooks=hooks,
        model=anthropic.model,
        max_tokens=orch_settings.max_tokens_per_turn,
        max_iterations=orch_settings.max_iterations,
        transcript=transcript,
    )
    return orch, sandbox, system_prompt


async def _run_turn(
    orch: Orchestrator, user_text: str, history: list[Message], system_prompt: str
) -> None:
    try:
        await orch.run(
            user_text,
            history,
            system_prompt=system_prompt,
            on_thinking_delta=_on_thinking,
            on_text_delta=_on_text,
        )
    except Exception as exc:  # provider/auth errors etc. — report, don't crash the REPL
        sys.stdout.write(_style(f"\n[error: {exc!r}]\n", "31"))
    sys.stdout.write("\n")
    sys.stdout.flush()


async def _amain(args: argparse.Namespace) -> None:
    anthropic = AnthropicSettings()
    orch_settings = OrchestratorSettings()
    sandbox_settings = SandboxSettings()
    orch, sandbox, system_prompt = _build(anthropic, orch_settings, sandbox_settings)

    try:
        await sandbox.start()
    except RuntimeError as exc:
        print(f"Sandbox startup failed: {exc}\nIs Docker running?", file=sys.stderr)
        raise SystemExit(1) from exc

    # Ctrl-C requests a graceful stop of the in-flight turn (not a hard exit).
    loop = asyncio.get_running_loop()
    with_signal = False
    try:
        loop.add_signal_handler(signal.SIGINT, orch.request_stop)
        with_signal = True
    except (NotImplementedError, RuntimeError):
        pass  # e.g. Windows / non-main thread — Ctrl-C falls back to KeyboardInterrupt

    history: list[Message] = []

    if args.task:
        await _run_turn(orch, args.task, history, system_prompt)
        return

    print(_dim("toolforge — type a task, or /new /reset /quit. Ctrl-C stops a running turn."))
    while True:
        try:
            user_text = (await asyncio.to_thread(input, "\n» ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_text:
            continue
        if user_text in ("/quit", "/exit"):
            break
        if user_text == "/new":
            history.clear()
            print(_dim("(history cleared)"))
            continue
        if user_text == "/reset":
            history.clear()
            sandbox.teardown()
            try:
                await sandbox.start()
            except RuntimeError as exc:
                # Keep the REPL alive — run() retries the start on next use.
                print(_style(f"[sandbox restart failed: {exc}]", "31"), file=sys.stderr)
            print(_dim("(history cleared, container recycled)"))
            continue
        await _run_turn(orch, user_text, history, system_prompt)

    if with_signal:
        loop.remove_signal_handler(signal.SIGINT)


def main() -> None:
    parser = argparse.ArgumentParser(prog="toolforge", description="Talk to the toolforge agent.")
    parser.add_argument(
        "task",
        nargs="?",
        help="A one-shot task to run. Omit for an interactive session.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_amain(args))
    except ValidationError as exc:
        # Almost always missing/invalid credentials or sandbox config.
        print(
            f"Configuration error:\n{exc}\n\n"
            "Copy .env.example to .env and fill in your credentials "
            "(see TOOLFORGE_ANTHROPIC_* / ANTHROPIC_API_KEY).",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
