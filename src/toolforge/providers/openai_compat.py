"""OpenAI-compatible Chat Completions adapter — the forge worker's model client.

Targets local OpenAI-compatible servers (vLLM, llama.cpp, LM Studio, Ollama)
at ``http://{host}:{port}/v1``. The ``IdMapper`` and the stream/send/drain
structure are ported from Zeemon's OpenAI adapter; the translation layer is new
— Zeemon speaks the Responses API, local servers speak Chat Completions.

Normalization contract: ``finish_reason`` is mapped into the Anthropic
``stop_reason`` vocabulary (see ``_FINISH_TO_STOP``) so the agent loop can
switch on the same strings regardless of provider. Tools are accepted in
Anthropic shape (``{name, description, input_schema}``) and translated to
OpenAI function-tool dicts internally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import openai

from toolforge.config import WorkerSettings
from toolforge.providers.base import (
    AuthMode,
    MessageEnd,
    PermanentProviderError,
    ProviderError,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
    TransientProviderError,
    drain_send,
    is_transient_status,
)
from toolforge.providers.messages import (
    ContentBlock,
    DocumentBlock,
    ImageBlock,
    Message,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from toolforge.providers.usage import UsageEvent, UsageHook, log_usage

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}


def _to_provider_error(exc: Exception) -> ProviderError:
    """Translate an escaped openai/httpx exception into the neutral taxonomy."""
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        body = getattr(exc, "body", None)
        err_type = body.get("error", {}).get("type") if isinstance(body, dict) else None
        if is_transient_status(status, err_type):
            return TransientProviderError(str(exc))
        return PermanentProviderError(str(exc))
    # APIConnectionError / httpx.TimeoutException: no HTTP status, always transient.
    return TransientProviderError(str(exc) or type(exc).__name__)


_MAX_STREAM_RETRIES = 5
_BASE_DELAY = 2.0

# Chat Completions finish_reason → Anthropic stop_reason vocabulary.
# Unknown values pass through unchanged.
_FINISH_TO_STOP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class IdMapper:
    """Per-stream Anthropic-style ↔ OpenAI tool-call id mapping.

    Canonical history uses Anthropic-shape ``toolu_...`` ids. The adapter
    mints ids inbound (translating ``call_...`` → ``toolu_...``) and
    translates outbound. The mapping is per-conversation: keep a fresh
    instance per ``stream()`` call.
    """

    def __init__(self) -> None:
        self._canon_to_openai: dict[str, str] = {}
        self._openai_to_canon: dict[str, str] = {}

    def bind_canonical_id(self, canonical_id: str, openai_id: str) -> None:
        self._canon_to_openai[canonical_id] = openai_id
        self._openai_to_canon[openai_id] = canonical_id

    def mint_for_openai(self, openai_id: str) -> str:
        if openai_id in self._openai_to_canon:
            return self._openai_to_canon[openai_id]
        canonical = f"toolu_{secrets.token_hex(8)}"
        self.bind_canonical_id(canonical, openai_id)
        return canonical

    def canonical_to_openai(self, canonical_id: str) -> str | None:
        return self._canon_to_openai.get(canonical_id)


def anthropic_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-shape tool schemas → OpenAI function-tool dicts."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def canonical_to_chat_messages(
    messages: list[Message], *, system: str, id_mapper: IdMapper
) -> list[dict[str, Any]]:
    """Translate canonical messages → Chat Completions message dicts.

    - The system prompt becomes the leading ``system`` message.
    - ``ToolResultBlock``s become ``role: "tool"`` messages (emitted before any
      remaining user content, as the API requires them directly after the
      assistant tool_calls message).
    - Assistant ``ToolUseBlock``s become ``tool_calls`` entries.
    - Thinking blocks are dropped outbound (same policy as the Zeemon adapter).
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            user_parts: list[dict[str, Any]] = []
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    openai_id = id_mapper.canonical_to_openai(b.tool_use_id)
                    if openai_id is None:
                        # Outbound-only path: mint a synthetic id.
                        openai_id = f"call_{secrets.token_hex(8)}"
                        id_mapper.bind_canonical_id(b.tool_use_id, openai_id)
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": openai_id,
                            "content": (
                                b.content if isinstance(b.content, str) else json.dumps(b.content)
                            ),
                        }
                    )
                elif isinstance(b, TextBlock):
                    user_parts.append({"type": "text", "text": b.text})
                elif isinstance(b, DocumentBlock):
                    user_parts.append(
                        {
                            "type": "text",
                            "text": f'<document title="{b.title}">\n{b.content}\n</document>',
                        }
                    )
                elif isinstance(b, ImageBlock):
                    user_parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{b.media_type};base64,{b.data}"},
                        }
                    )
                # else: drop other block types from user messages (defensive)
            if user_parts:
                if all(p["type"] == "text" for p in user_parts):
                    content: str | list[dict[str, Any]] = "\n".join(p["text"] for p in user_parts)
                else:
                    content = user_parts
                out.append({"role": "user", "content": content})
        else:  # assistant
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for b in m.content:
                if isinstance(b, TextBlock):
                    text_parts.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    openai_id = id_mapper.canonical_to_openai(b.id)
                    if openai_id is None:
                        openai_id = f"call_{secrets.token_hex(8)}"
                        id_mapper.bind_canonical_id(b.id, openai_id)
                    tool_calls.append(
                        {
                            "id": openai_id,
                            "type": "function",
                            "function": {"name": b.name, "arguments": json.dumps(b.input)},
                        }
                    )
                # ThinkingBlock / RedactedThinkingBlock: drop outbound
                elif not isinstance(b, ThinkingBlock | RedactedThinkingBlock):
                    logger.debug("dropping assistant block outbound: %s", type(b).__name__)
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                assistant_msg["content"] = "".join(text_parts)
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            if "content" in assistant_msg or "tool_calls" in assistant_msg:
                out.append(assistant_msg)
    return out


class OpenAICompatClient:
    """Chat Completions adapter for local OpenAI-compatible servers."""

    name: str = "openai"

    def __init__(self, settings: WorkerSettings, usage_hook: UsageHook | None = None) -> None:
        self.settings = settings
        self.auth_mode = AuthMode.API_KEY
        self._usage_hook: UsageHook = usage_hook or log_usage

    def _build_sdk_client(self) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key=self.settings.api_key.get_secret_value() or "EMPTY",
            base_url=self.settings.base_url,
            max_retries=0,  # retries are handled in stream()
        )

    async def _emit_usage(self, event: UsageEvent) -> None:
        """Invoke the usage hook; a broken hook must never abort a model turn."""
        try:
            await self._usage_hook(event)
        except Exception:
            logger.warning("usage hook raised; ignoring", exc_info=True)

    async def _drain_sdk_stream(
        self,
        raw_stream: Any,
        *,
        id_mapper: IdMapper,
        tool_acc: dict[int, dict[str, Any]],
        final_state: dict[str, Any],
        ts_start: float,
        model: str,
        extra: dict[str, Any] | None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Drain a raw Chat Completions chunk stream, yielding ProviderEvents.

        Mutates *tool_acc* and *final_state* in-place. The usage hook and
        MessageEnd are emitted here on successful completion. Cooperatively
        aborts when *cancel_event* is set.
        """
        async for chunk in raw_stream:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError("cancelled by stop event")
            ck: Any = chunk

            usage: Any = getattr(ck, "usage", None)
            if usage is not None:
                final_state["input_tokens"] = int(getattr(usage, "prompt_tokens", 0) or 0)
                final_state["output_tokens"] = int(getattr(usage, "completion_tokens", 0) or 0)

            choices: Any = getattr(ck, "choices", None) or []
            if not choices:
                continue
            choice: Any = choices[0]

            delta: Any = getattr(choice, "delta", None)
            if delta is not None:
                # vLLM/llama.cpp reasoning-parser extension (Qwen-style models).
                reasoning = getattr(delta, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning:
                    final_state.setdefault("thinking_parts", []).append(reasoning)
                    yield ThinkingDelta(text=reasoning)

                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    final_state.setdefault("text_parts", []).append(content)
                    yield TextDelta(text=content)

                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = int(getattr(tc, "index", 0) or 0)
                    fn: Any = getattr(tc, "function", None)
                    if idx not in tool_acc:
                        openai_id = str(getattr(tc, "id", None) or f"call_{secrets.token_hex(8)}")
                        name = str(getattr(fn, "name", None) or "") if fn is not None else ""
                        canonical_id = id_mapper.mint_for_openai(openai_id)
                        tool_acc[idx] = {
                            "canonical_id": canonical_id,
                            "name": name,
                            "arguments": "",
                        }
                        yield ToolUseStart(id=canonical_id, name=name)
                    args = getattr(fn, "arguments", None) if fn is not None else None
                    if isinstance(args, str) and args:
                        tool_acc[idx]["arguments"] += args
                        yield ToolUseDelta(partial_json=args)

            finish = getattr(choice, "finish_reason", None)
            if finish:
                final_state["finish_reason"] = str(finish)

        # Stream exhausted — finalize.
        for idx in sorted(tool_acc):
            yield ToolUseEnd(id=tool_acc[idx]["canonical_id"])

        latency_ms = int((time.monotonic() - ts_start) * 1000)

        blocks: list[ContentBlock] = []
        thinking = "".join(final_state.get("thinking_parts", []))
        if thinking:
            blocks.append(ThinkingBlock(thinking=thinking, signature=None))
        text = "".join(final_state.get("text_parts", []))
        if text:
            blocks.append(TextBlock(text=text))
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            try:
                tool_input: dict[str, Any] = json.loads(acc["arguments"] or "{}")
            except json.JSONDecodeError:
                logger.warning(
                    "openai_compat.tool_args_parse_failed name=%s args=%.500s",
                    acc["name"],
                    acc["arguments"],
                )
                tool_input = {}
            blocks.append(ToolUseBlock(id=acc["canonical_id"], name=acc["name"], input=tool_input))

        finish = final_state.get("finish_reason")
        stop_reason: str | None = _FINISH_TO_STOP.get(finish, finish) if finish else None
        # Some servers (notably llama.cpp) report "stop" even when the turn
        # produced tool calls — normalize so the loop sees "tool_use".
        if tool_acc and stop_reason == "end_turn":
            stop_reason = "tool_use"

        input_tokens: int = final_state.get("input_tokens", 0)
        output_tokens: int = final_state.get("output_tokens", 0)
        usage_obj: Usage | None = None
        if input_tokens or output_tokens:
            usage_obj = Usage(input_tokens=input_tokens, output_tokens=output_tokens)

        msg = Message(
            role="assistant",
            content=blocks,
            ts=datetime.now(tz=UTC),
            provider="openai",
            auth_mode=self.auth_mode.value,
            model=model,
            usage=usage_obj,
            stop_reason=stop_reason,
            latency_ms=latency_ms,
        )

        turn_id = (extra or {}).get("turn_id")
        await self._emit_usage(
            UsageEvent(
                provider=self.name,
                auth_mode=self.auth_mode.value,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                stop_reason=stop_reason,
                component=(extra or {}).get("component", "forge_worker"),
                turn_id=turn_id if isinstance(turn_id, UUID) else None,
            )
        )

        yield MessageEnd(message=msg)

    async def stream(
        self,
        *,
        messages: list[Message],
        system: str,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int,
        cancel_event: asyncio.Event | None = None,
        turn_id: UUID | None = None,
        component: str = "forge_worker",
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        # Merge turn_id/component into extra so the usage-hook path picks them up.
        extra = dict(extra or {})
        if turn_id is not None:
            extra.setdefault("turn_id", turn_id)
        extra.setdefault("component", component)

        id_mapper = IdMapper()
        chat_messages = canonical_to_chat_messages(messages, system=system, id_mapper=id_mapper)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = anthropic_tools_to_openai(tools)

        # Local servers (vLLM warm-up, llama.cpp model load) drop connections
        # transiently — retry with the same ladder as the Anthropic adapter.
        # Retries are only safe while nothing has been delivered to the
        # consumer: a retried attempt re-samples the response from scratch, so
        # replaying it after events already reached on_text_delta/-thinking
        # callbacks would duplicate/garble live output. Once `delivered` is
        # set, any failure surfaces to the caller instead.
        last_exc: Exception | None = None
        delivered = False
        for attempt in range(_MAX_STREAM_RETRIES + 1):
            tool_acc: dict[int, dict[str, Any]] = {}
            final_state: dict[str, Any] = {}
            ts_start = time.monotonic()
            client = self._build_sdk_client()
            try:
                raw_stream = await client.chat.completions.create(**kwargs)
                async for ev in self._drain_sdk_stream(
                    raw_stream,
                    id_mapper=id_mapper,
                    tool_acc=tool_acc,
                    final_state=final_state,
                    ts_start=ts_start,
                    model=model,
                    extra=extra,
                    cancel_event=cancel_event,
                ):
                    yield ev
                    delivered = True
                return
            except openai.APIStatusError as exc:
                if delivered or exc.status_code not in _RETRYABLE_STATUS:
                    raise
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "openai_compat.stream.retry status=%d attempt=%d delay_s=%.1f",
                    exc.status_code,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            except openai.APIConnectionError as exc:
                if delivered:
                    raise
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "openai_compat.stream.retry error=%s attempt=%d delay_s=%.1f",
                    exc,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            except httpx.TimeoutException as exc:
                # A raw httpx timeout escaping mid-stream is not wrapped by
                # the SDK. Retryable only pre-delivery (see `delivered`
                # above); once events reached the consumer the timeout
                # surfaces instead.
                if delivered:
                    raise
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "openai_compat.stream.retry error=%s attempt=%d delay_s=%.1f",
                    type(exc).__name__,  # str(exc) is empty for timeouts
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc

    async def send(
        self,
        *,
        messages: list[Message],
        system: str,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int,
        cancel_event: asyncio.Event | None = None,
        turn_id: UUID | None = None,
        component: str = "forge_worker",
        extra: dict[str, Any] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Message:
        # Translate escaped SDK exceptions into the neutral provider taxonomy.
        # asyncio.CancelledError (a BaseException) is not caught here.
        try:
            return await drain_send(
                self.stream(
                    messages=messages,
                    system=system,
                    model=model,
                    tools=tools,
                    max_tokens=max_tokens,
                    cancel_event=cancel_event,
                    turn_id=turn_id,
                    component=component,
                    extra=extra,
                ),
                on_thinking_delta=on_thinking_delta,
                on_text_delta=on_text_delta,
            )
        except (
            openai.APIStatusError,
            openai.APIConnectionError,
            httpx.TimeoutException,
        ) as exc:
            raise _to_provider_error(exc) from exc
