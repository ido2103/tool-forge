"""The orchestrator agent loop — send → handle tool calls → repeat until text.

Ported from Zeemon ``core/agent.py`` (the ~⅓ that is the reusable spine) and
retargeted onto toolforge's provider taxonomy and instance registry. What was
dropped: session persistence, memory injection, the approval gate, cron
handling, and orphaned-tool-use repair (toolforge's provider layer already does
the last one in ``_anthropic_sanitize.py``).

State model: the loop is stateless per call. History is a plain
``list[Message]`` the caller owns (the REPL), mutated in place and mirrored to a
:class:`Transcript`. The tool set is re-read from the registry every iteration,
so a tool the forge registers mid-task is callable on the next turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from toolforge.orchestrator.hooks import HookEvent, HookManager
from toolforge.orchestrator.transcript import Transcript
from toolforge.providers import (
    Message,
    ProviderClient,
    TextBlock,
    ToolResultBlock,
    TransientProviderError,
)
from toolforge.registry import ToolCall, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

_BASE64_RE = re.compile(r'"[A-Za-z0-9+/=]{100,}"')

_ITERATION_LIMIT_NUDGE = (
    "[System: iteration limit reached] You have used all available tool-call "
    "iterations for this turn. Respond to the user now in plain text: summarize "
    "what you accomplished and note any remaining steps."
)

_REFUSAL_TEXT = "I'm unable to help with that request."
_INTERRUPTED_TEXT = "Stopping."


class AgentError(Exception):
    """Raised on an unexpected ``stop_reason`` the loop does not know how to handle."""


def _truncate(s: str | Any, max_len: int = 300) -> str:
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=False)
    return s[:max_len] + "…" if len(s) > max_len else s


def _sanitize_tool_input(inp: dict[str, Any], max_len: int = 200) -> str:
    s = json.dumps(inp, ensure_ascii=False)
    s = _BASE64_RE.sub('"<base64 data>"', s)
    return s[:max_len] + "…" if len(s) > max_len else s


class Orchestrator:
    """Runs one user turn to a final text answer, calling tools in a loop."""

    # One long-pause second chance after the provider's own fast retries are
    # exhausted. Patchable to ~0 in tests.
    _TRANSIENT_DELAY_S = 60.0

    def __init__(
        self,
        *,
        client: ProviderClient,
        registry: ToolRegistry,
        hooks: HookManager,
        model: str,
        max_tokens: int,
        max_iterations: int,
        transcript: Transcript | None = None,
        component: str = "orchestrator",
    ) -> None:
        self._client = client
        self._registry = registry
        self._hooks = hooks
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._transcript = transcript
        self._component = component
        self._active_runs: set[asyncio.Event] = set()

    # ── Cancel-event lifecycle ──────────────────────────────────────────────

    def create_cancel_event(self) -> asyncio.Event:
        ev = asyncio.Event()
        self._active_runs.add(ev)
        return ev

    def remove_cancel_event(self, ev: asyncio.Event) -> None:
        self._active_runs.discard(ev)

    def request_stop(self) -> None:
        """Signal every active turn to abort. Idempotent."""
        for ev in self._active_runs:
            ev.set()
        logger.info("stop requested for %d active run(s)", len(self._active_runs))

    # ── History helper ──────────────────────────────────────────────────────

    def _append(self, history: list[Message], msg: Message) -> None:
        history.append(msg)
        if self._transcript is not None:
            self._transcript.append(msg)

    # ── LLM send with agent-level retry ─────────────────────────────────────

    async def _send_with_retry(
        self,
        kwargs: dict[str, Any],
        cancel_event: asyncio.Event,
        *,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Message:
        """Call ``client.send``, retrying once on a transient provider error.

        The provider already retries fast; this adds one long-pause chance for a
        sustained but brief outage. ``PermanentProviderError`` is not caught and
        propagates immediately.
        """
        try:
            return await self._client.send(
                **kwargs,
                on_thinking_delta=on_thinking_delta,
                on_text_delta=on_text_delta,
            )
        except TransientProviderError as exc:
            logger.warning(
                "transient provider error; retrying in %.0fs: %s",
                self._TRANSIENT_DELAY_S,
                str(exc)[:200],
            )
            await asyncio.sleep(self._TRANSIENT_DELAY_S)
            if cancel_event.is_set():
                raise
            return await self._client.send(
                **kwargs,
                on_thinking_delta=on_thinking_delta,
                on_text_delta=on_text_delta,
            )

    # ── Tool execution ──────────────────────────────────────────────────────

    async def _execute_tools(
        self,
        tool_calls: list[ToolCall],
        cancel_event: asyncio.Event | None = None,
    ) -> list[ToolResult]:
        """Run tools concurrently. On cancel, fire cancel-handlers and synthesize
        ``[ABORTED]`` results. A handler that raises becomes an ``is_error`` result;
        it never aborts the loop.
        """
        if not tool_calls:
            return []

        self._registry.context.reset_cancel_handlers()

        async def _run_one(tc: ToolCall) -> ToolResult:
            logger.info("tool call: %s(%s)", tc.name, _sanitize_tool_input(tc.input))
            await self._hooks.fire(
                HookEvent.ON_TOOL_PRE_EXECUTE,
                tool_name=tc.name,
                call_id=tc.id,
                input=tc.input,
                component=self._component,
            )
            t0 = time.perf_counter()
            try:
                result = await self._registry.execute(tc.name, tc.input)
                result.tool_use_id = tc.id
                elapsed_ms = round((time.perf_counter() - t0) * 1000)
                logger.info(
                    "tool result: %s is_error=%s (%dms) %s",
                    tc.name,
                    result.is_error,
                    elapsed_ms,
                    _truncate(result.content),
                )
                await self._hooks.fire(
                    HookEvent.ON_TOOL_POST_EXECUTE,
                    tool_name=tc.name,
                    call_id=tc.id,
                    is_error=result.is_error,
                    latency_ms=elapsed_ms,
                    component=self._component,
                )
                return result
            except Exception as e:
                elapsed_ms = round((time.perf_counter() - t0) * 1000)
                logger.warning("tool error: %s (%dms)", tc.name, elapsed_ms, exc_info=True)
                await self._hooks.fire(
                    HookEvent.ON_TOOL_POST_EXECUTE,
                    tool_name=tc.name,
                    call_id=tc.id,
                    is_error=True,
                    latency_ms=elapsed_ms,
                    component=self._component,
                )
                return ToolResult(
                    tool_use_id=tc.id,
                    content=f"[Tool '{tc.name}' failed: {e!r}]",
                    is_error=True,
                )

        tasks = {tc.id: asyncio.create_task(_run_one(tc)) for tc in tool_calls}

        if cancel_event is not None:
            stop_waiter = asyncio.create_task(cancel_event.wait())
            await asyncio.wait([*tasks.values(), stop_waiter], return_when=asyncio.FIRST_COMPLETED)
            if cancel_event.is_set():
                for t in tasks.values():
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
                await self._registry.context.fire_cancel_handlers()
                results: list[ToolResult] = []
                for tc in tool_calls:
                    task = tasks[tc.id]
                    if task.done() and not task.cancelled():
                        try:
                            results.append(task.result())
                            continue
                        except Exception:
                            pass
                    results.append(
                        ToolResult(
                            tool_use_id=tc.id,
                            content="[ABORTED by emergency stop]",
                            is_error=True,
                        )
                    )
                stop_waiter.cancel()
                return self._merge_results(tool_calls, results)
            stop_waiter.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        else:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

        executed = [tasks[tc.id].result() for tc in tool_calls]
        return self._merge_results(tool_calls, executed)

    @staticmethod
    def _merge_results(
        original_order: list[ToolCall],
        executed: list[ToolResult],
    ) -> list[ToolResult]:
        """Re-assemble results in the original tool_calls order."""
        by_id = {r.tool_use_id: r for r in executed}
        return [by_id[tc.id] for tc in original_order]

    # ── Finish path: synthesize "Stopping." on cancel ───────────────────────

    async def _finish_interrupted(self, history: list[Message]) -> str:
        msg = Message(
            role="assistant",
            content=[TextBlock(text=_INTERRUPTED_TEXT)],
            ts=datetime.now(tz=UTC),
            stop_reason="interrupted",
        )
        self._append(history, msg)
        await self._hooks.fire(HookEvent.ON_RESPONSE, text=_INTERRUPTED_TEXT)
        return _INTERRUPTED_TEXT

    # ── Main loop ────────────────────────────────────────────────────────────

    async def run(
        self,
        user_text: str,
        history: list[Message],
        *,
        system_prompt: str,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Run one user turn to a final assistant text answer.

        Appends the user message and every assistant/tool message to *history*
        in place. Returns the final text (``""`` never; a refusal/interrupt
        returns its canned text).
        """
        turn_id = uuid4()
        self._registry.context.turn_id = turn_id

        self._append(
            history,
            Message(role="user", content=[TextBlock(text=user_text)], ts=datetime.now(tz=UTC)),
        )

        cancel_event = self.create_cancel_event()
        try:
            for iteration in range(1, self._max_iterations + 1):
                if cancel_event.is_set():
                    return await self._finish_interrupted(history)
                await self._hooks.fire(HookEvent.ON_ITERATION, iteration=iteration)

                send_kwargs: dict[str, Any] = dict(
                    messages=history,
                    system=system_prompt,
                    model=self._model,
                    tools=self._registry.get_schemas() or None,
                    max_tokens=self._max_tokens,
                    cancel_event=cancel_event,
                    turn_id=turn_id,
                    component=self._component,
                )
                try:
                    response = await self._send_with_retry(
                        send_kwargs,
                        cancel_event,
                        on_thinking_delta=on_thinking_delta,
                        on_text_delta=on_text_delta,
                    )
                except asyncio.CancelledError:
                    return await self._finish_interrupted(history)

                if cancel_event.is_set():
                    return await self._finish_interrupted(history)

                # SSE-truncation override: a stream cut off mid-tool-call can
                # arrive with a non-tool_use stop_reason but real tool_use blocks.
                if response.stop_reason != "tool_use" and response.tool_use_blocks:
                    logger.warning("stop_reason override: %s -> tool_use", response.stop_reason)
                    response.stop_reason = "tool_use"

                stop_reason = response.stop_reason

                if stop_reason == "end_turn":
                    self._append(history, response)
                    await self._hooks.fire(HookEvent.ON_RESPONSE, text=response.text)
                    return response.text

                if stop_reason == "tool_use":
                    self._append(history, response)
                    if response.text:
                        await self._hooks.fire(HookEvent.ON_INTERMEDIATE_TEXT, text=response.text)
                    tool_calls = [
                        ToolCall(id=b.id, name=b.name, input=b.input)
                        for b in response.tool_use_blocks
                    ]
                    results = await self._execute_tools(tool_calls, cancel_event)
                    self._append(
                        history,
                        Message(
                            role="user",
                            content=[
                                ToolResultBlock(
                                    tool_use_id=r.tool_use_id,
                                    content=r.content,
                                    is_error=r.is_error,
                                )
                                for r in results
                            ],
                            ts=datetime.now(tz=UTC),
                        ),
                    )
                    if cancel_event.is_set():
                        return await self._finish_interrupted(history)
                    continue

                if stop_reason in ("max_tokens", "model_context_window_exceeded"):
                    self._append(history, response)
                    await self._hooks.fire(HookEvent.ON_RESPONSE, text=response.text)
                    return response.text

                if stop_reason == "pause_turn":
                    self._append(history, response)
                    continue

                if stop_reason == "stop_sequence":
                    self._append(history, response)
                    await self._hooks.fire(HookEvent.ON_RESPONSE, text=response.text)
                    return response.text

                if stop_reason == "refusal":
                    refusal_msg = Message(
                        role="assistant",
                        content=[TextBlock(text=_REFUSAL_TEXT)],
                        ts=datetime.now(tz=UTC),
                        stop_reason="refusal",
                    )
                    self._append(history, refusal_msg)
                    await self._hooks.fire(HookEvent.ON_RESPONSE, text=_REFUSAL_TEXT)
                    return _REFUSAL_TEXT

                raise AgentError(f"unexpected stop_reason: {stop_reason!r}")

            # Iterations exhausted — one final wrap-up call with no tools.
            logger.warning("iteration limit (%d) reached; wrapping up", self._max_iterations)
            self._append(
                history,
                Message(
                    role="user",
                    content=[TextBlock(text=_ITERATION_LIMIT_NUDGE)],
                    ts=datetime.now(tz=UTC),
                ),
            )
            wrapup_kwargs: dict[str, Any] = dict(
                messages=history,
                system=system_prompt,
                model=self._model,
                tools=None,
                max_tokens=self._max_tokens,
                cancel_event=cancel_event,
                turn_id=turn_id,
                component=self._component,
            )
            try:
                wrapup = await self._send_with_retry(
                    wrapup_kwargs,
                    cancel_event,
                    on_thinking_delta=on_thinking_delta,
                    on_text_delta=on_text_delta,
                )
            except asyncio.CancelledError:
                return await self._finish_interrupted(history)
            self._append(history, wrapup)
            await self._hooks.fire(HookEvent.ON_RESPONSE, text=wrapup.text)
            return wrapup.text
        finally:
            self.remove_cancel_event(cancel_event)
