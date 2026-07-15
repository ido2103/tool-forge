"""Anthropic Messages-API adapter — the orchestrator's model client.

Ported from Zeemon ``providers/anthropic.py``. Adaptations: Postgres cost
ledger replaced by the pluggable usage hook (``providers/usage.py``); config
comes from :class:`toolforge.config.AnthropicSettings`; OAuth token I/O runs in
a thread so it never blocks the event loop; thinking display is hardcoded to
``"summarized"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import anthropic
import httpx

from toolforge.config import AnthropicSettings
from toolforge.providers._anthropic_sanitize import (
    fix_orphaned_tool_uses,
    sanitize_messages_for_claude,
)
from toolforge.providers.base import (
    AuthMode,
    MessageEnd,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
    drain_send,
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
from toolforge.providers.oauth_anthropic import (
    _atomic_write_creds,
    read_or_refresh,
    refresh_anthropic_oauth,
)
from toolforge.providers.usage import UsageEvent, UsageHook, log_usage

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}
_MAX_STREAM_RETRIES = 5
_BASE_DELAY = 2.0

# Masquerade constants for OAuth mode — matches Hermes' _COMMON_BETAS + _OAUTH_ONLY_BETAS.
# Without these Anthropic's OAuth routing intermittently 500s.
_OAUTH_DEFAULT_BETAS = ",".join(
    [
        "interleaved-thinking-2025-05-14",
        "fine-grained-tool-streaming-2025-05-14",
        "claude-code-20250219",
        "oauth-2025-04-20",
    ]
)
_CLAUDE_CODE_VERSION_PRETENDED = "2.1.74"
# Required as the first system block on every OAuth-piggyback call. Without it,
# Anthropic refuses the request with a vague 429 rate_limit_error (not a 401),
# even when the subscription has plenty of quota left.
_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def canonical_to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate toolforge canonical messages → Anthropic Messages-API dicts.

    1. Convert each canonical block to its Anthropic dict shape.
    2. Run the sanitizer (strip signature-less thinking + foreign keys).
    3. Run the orphan-tool-use repair (inject synthetic error tool_results).
    """
    raw: list[dict[str, Any]] = []
    for m in messages:
        blocks: list[dict[str, Any]] = []
        for b in m.content:
            translated = _block_to_anthropic(b)
            if translated is not None:
                blocks.append(translated)
        raw.append({"role": m.role, "content": blocks})

    raw = sanitize_messages_for_claude(raw)
    fix_orphaned_tool_uses(raw)
    return raw


def _block_to_anthropic(b: ContentBlock) -> dict[str, Any] | None:
    if isinstance(b, TextBlock):
        if not b.text or not b.text.strip():
            return None
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": b.tool_use_id,
            "content": b.content,
            "is_error": b.is_error,
        }
    if isinstance(b, ThinkingBlock):
        out: dict[str, Any] = {"type": "thinking", "thinking": b.thinking}
        if b.signature is not None:
            out["signature"] = b.signature
        return out
    if isinstance(b, RedactedThinkingBlock):
        return {"type": "redacted_thinking", "data": b.data}
    if isinstance(b, DocumentBlock):
        return {
            "type": "document",
            "source": {
                "type": b.source_type,
                "media_type": b.media_type,
                "data": b.content,
            },
            "title": b.title,
        }
    if isinstance(b, ImageBlock):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": b.media_type,
                "data": b.data,
            },
        }
    raise TypeError(f"unhandled canonical block type: {type(b).__name__}")


def anthropic_to_canonical(
    raw: dict[str, Any],
    *,
    ts: datetime,
    auth_mode: str | None = None,
    latency_ms: int | None = None,
) -> Message:
    """Translate an Anthropic Messages-API response dict → canonical Message."""
    blocks: list[ContentBlock] = []
    for b in raw.get("content", []):
        btype = b.get("type")
        if btype == "text":
            blocks.append(TextBlock(text=b["text"]))
        elif btype == "tool_use":
            blocks.append(ToolUseBlock(id=b["id"], name=b["name"], input=b.get("input", {})))
        elif btype == "thinking":
            blocks.append(ThinkingBlock(thinking=b["thinking"], signature=b.get("signature")))
        elif btype == "redacted_thinking":
            blocks.append(RedactedThinkingBlock(data=b["data"]))
        else:
            # Unknown block type — preserve as text fallback to avoid data loss
            blocks.append(TextBlock(text=f"[unhandled anthropic block: {btype!r}]"))

    usage: Usage | None = None
    if (u := raw.get("usage")) is not None:
        usage = Usage(
            input_tokens=u["input_tokens"],
            output_tokens=u["output_tokens"],
            cache_creation_input_tokens=u.get("cache_creation_input_tokens"),
            cache_read_input_tokens=u.get("cache_read_input_tokens"),
        )

    return Message(
        role=raw["role"],
        content=blocks,
        ts=ts,
        provider="anthropic",
        auth_mode=auth_mode,
        model=raw.get("model"),
        usage=usage,
        stop_reason=raw.get("stop_reason"),
        latency_ms=latency_ms,
    )


def _force_refresh_token(creds_path: Path) -> str:
    """Synchronous force-refresh: rotate the token pair and return the access token."""
    creds: dict[str, Any] = json.loads(creds_path.read_text())
    new_creds = refresh_anthropic_oauth(str(creds["refreshToken"]))
    _atomic_write_creds(creds_path, new_creds)
    return str(new_creds["accessToken"])


class AnthropicClient:
    """Anthropic Messages-API adapter — streaming canonical, both auth modes."""

    name: str = "anthropic"

    def __init__(self, settings: AnthropicSettings, usage_hook: UsageHook | None = None) -> None:
        self.settings = settings
        self.auth_mode = AuthMode(settings.auth_mode)
        self._usage_hook: UsageHook = usage_hook or log_usage

    async def _build_sdk_client(
        self,
        *,
        force_refresh: bool = False,
        max_retries: int = 5,
    ) -> anthropic.AsyncAnthropic:
        if self.auth_mode is AuthMode.API_KEY:
            if self.settings.api_key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is required when auth_mode='api_key'")
            return anthropic.AsyncAnthropic(
                api_key=self.settings.api_key.get_secret_value(),
                base_url=self.settings.base_url or None,
                max_retries=max_retries,
            )
        # OAuth path — token I/O is sync (httpx.Client + file ops); run off-loop.
        creds_path = self.settings.oauth_credentials_path
        if force_refresh:
            token = await asyncio.to_thread(_force_refresh_token, creds_path)
        else:
            token = await asyncio.to_thread(read_or_refresh, creds_path)
        return anthropic.AsyncAnthropic(
            auth_token=token,
            base_url=self.settings.base_url or None,
            max_retries=max_retries,
            default_headers={
                "anthropic-beta": _OAUTH_DEFAULT_BETAS,
                "user-agent": f"claude-cli/{_CLAUDE_CODE_VERSION_PRETENDED} (external, cli)",
                "x-app": "cli",
            },
        )

    async def _emit_usage(self, event: UsageEvent) -> None:
        """Invoke the usage hook; a broken hook must never abort a model turn."""
        try:
            await self._usage_hook(event)
        except Exception:
            logger.warning("usage hook raised; ignoring", exc_info=True)

    async def _drain_sdk_stream(
        self,
        sdk_stream: Any,
        *,
        tool_ids: dict[int, str],
        final_state: dict[str, Any],
        ts_start: float,
        model: str,
        extra: dict[str, Any] | None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Drain a single SDK stream context manager, yielding ProviderEvents.

        Mutates *tool_ids* and *final_state* in-place so the caller (stream())
        can access accumulated token counts / stop-reason on completion.
        Cooperatively aborts the stream when *cancel_event* is set.
        """
        # Track tool input JSON chunks for diagnostics — the SDK's non-beta
        # streaming path swallows the buffer on from_json parse failure
        # (anthropic-sdk #1265).
        json_bufs: dict[int, list[str]] = {}

        async with sdk_stream as s:
            try:
                async for raw in s:
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError("cancelled by stop event")
                    ev: Any = raw
                    t: str = ev.type

                    if t == "message_start":
                        u = ev.message.usage
                        if u is not None:
                            final_state["input_tokens"] = u.input_tokens
                            final_state["cache_creation_tokens"] = getattr(
                                u, "cache_creation_input_tokens", None
                            )
                            final_state["cache_read_tokens"] = getattr(
                                u, "cache_read_input_tokens", None
                            )

                    elif t == "content_block_start":
                        cb = ev.content_block
                        idx: int = ev.index
                        if cb.type == "tool_use":
                            tool_ids[idx] = cb.id
                            yield ToolUseStart(id=cb.id, name=cb.name)

                    elif t == "content_block_delta":
                        delta: Any = ev.delta
                        dt: str = delta.type
                        if dt == "text_delta":
                            yield TextDelta(text=delta.text)
                        elif dt == "thinking_delta":
                            yield ThinkingDelta(text=delta.thinking)
                        elif dt == "input_json_delta":
                            json_bufs.setdefault(ev.index, []).append(delta.partial_json)
                            yield ToolUseDelta(partial_json=delta.partial_json)

                    elif t == "content_block_stop":
                        idx = ev.index
                        if idx in tool_ids:
                            yield ToolUseEnd(id=tool_ids[idx])

                    elif t == "message_delta":
                        final_state["stop_reason"] = ev.delta.stop_reason
                        u2 = ev.usage
                        if u2 is not None:
                            final_state["output_tokens"] = u2.output_tokens
                            cct = getattr(u2, "cache_creation_input_tokens", None)
                            if cct is not None:
                                final_state["cache_creation_tokens"] = cct
                            crt = getattr(u2, "cache_read_input_tokens", None)
                            if crt is not None:
                                final_state["cache_read_tokens"] = crt

                    elif t == "message_stop":
                        acc = s.current_message_snapshot
                        latency_ms = int((time.monotonic() - ts_start) * 1000)

                        canon_blocks: list[ContentBlock] = []
                        for blk in acc.content:
                            b: Any = blk
                            if b.type == "text":
                                canon_blocks.append(TextBlock(text=b.text))
                            elif b.type == "tool_use":
                                raw_input: dict[str, Any] = dict(b.input)
                                canon_blocks.append(
                                    ToolUseBlock(id=b.id, name=b.name, input=raw_input)
                                )
                            elif b.type == "thinking":
                                canon_blocks.append(
                                    ThinkingBlock(
                                        thinking=b.thinking,
                                        signature=getattr(b, "signature", None),
                                    )
                                )
                            elif b.type == "redacted_thinking":
                                canon_blocks.append(RedactedThinkingBlock(data=b.data))

                        input_tokens: int = final_state.get("input_tokens", 0)
                        output_tokens: int = final_state.get("output_tokens", 0)
                        usage: Usage | None = None
                        if input_tokens or output_tokens:
                            usage = Usage(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cache_creation_input_tokens=final_state.get(
                                    "cache_creation_tokens"
                                ),
                                cache_read_input_tokens=final_state.get("cache_read_tokens"),
                            )

                        final_msg = Message(
                            role="assistant",
                            content=canon_blocks,
                            ts=datetime.now(tz=UTC),
                            provider="anthropic",
                            auth_mode=self.auth_mode.value,
                            model=model,
                            usage=usage,
                            stop_reason=final_state.get("stop_reason"),
                            latency_ms=latency_ms,
                        )

                        logger.info(
                            "api.response stop_reason=%s input_tokens=%d output_tokens=%d "
                            "cache_read=%d cache_creation=%d latency_ms=%d model=%s component=%s",
                            final_state.get("stop_reason"),
                            input_tokens,
                            output_tokens,
                            final_state.get("cache_read_tokens") or 0,
                            final_state.get("cache_creation_tokens") or 0,
                            latency_ms,
                            model,
                            (extra or {}).get("component", "orchestrator"),
                        )

                        turn_id = (extra or {}).get("turn_id")
                        await self._emit_usage(
                            UsageEvent(
                                provider=self.name,
                                auth_mode=self.auth_mode.value,
                                model=model,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cache_read_tokens=final_state.get("cache_read_tokens") or 0,
                                cache_creation_tokens=final_state.get("cache_creation_tokens") or 0,
                                latency_ms=latency_ms,
                                stop_reason=final_state.get("stop_reason"),
                                component=(extra or {}).get("component", "orchestrator"),
                                turn_id=turn_id if isinstance(turn_id, UUID) else None,
                            )
                        )

                        yield MessageEnd(message=final_msg)
            except ValueError as exc:
                logger.error(
                    "anthropic.stream.json_parse_failed error=%s tool_ids=%s json_buffers=%s",
                    exc,
                    tool_ids,
                    {idx: "".join(chunks)[:500] for idx, chunks in json_bufs.items()},
                )
                raise

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
        component: str = "orchestrator",
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        # Merge turn_id/component into extra so the usage-hook path picks them up.
        extra = dict(extra or {})
        if turn_id is not None:
            extra.setdefault("turn_id", turn_id)
        extra.setdefault("component", component)

        anth_messages = canonical_to_anthropic(messages)

        cache_control: dict[str, Any] = {"type": "ephemeral"}
        if self.settings.cache_ttl == "1h":
            cache_control["ttl"] = "1h"

        if self.auth_mode is AuthMode.OAUTH:
            system_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX},
                {"type": "text", "text": system},
            ]
        else:
            system_blocks = [{"type": "text", "text": system}]

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": anth_messages,
            "cache_control": cache_control,
        }
        if tools:
            kwargs["tools"] = tools

        if self.settings.extended_thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

        last_exc: Exception | None = None
        oauth_refreshed = False
        for attempt in range(_MAX_STREAM_RETRIES + 1):
            tool_ids: dict[int, str] = {}
            final_state: dict[str, Any] = {}
            ts_start = time.monotonic()
            client = await self._build_sdk_client(
                force_refresh=oauth_refreshed,
                max_retries=0,
            )
            try:
                async for ev in self._drain_sdk_stream(
                    client.messages.stream(**kwargs),
                    tool_ids=tool_ids,
                    final_state=final_state,
                    ts_start=ts_start,
                    model=model,
                    extra=extra,
                    cancel_event=cancel_event,
                ):
                    yield ev
                return
            except anthropic.AuthenticationError:
                if self.auth_mode is not AuthMode.OAUTH or oauth_refreshed:
                    raise
                oauth_refreshed = True
                continue
            except anthropic.APIStatusError as exc:
                if exc.status_code not in _RETRYABLE_STATUS:
                    raise
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "anthropic.stream.retry status=%d attempt=%d delay_s=%.1f",
                    exc.status_code,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            except anthropic.APIConnectionError as exc:
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "anthropic.stream.retry error=%s attempt=%d delay_s=%.1f",
                    exc,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            except httpx.TimeoutException as exc:
                # A raw httpx read/connect timeout that escapes mid-stream is
                # not wrapped by the SDK in APIConnectionError. Retrying is
                # safe: send() ignores partial events and the usage hook /
                # MessageEnd only fire at message_stop, which a timed-out
                # attempt never reaches.
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "anthropic.stream.retry error=%s attempt=%d delay_s=%.1f",
                    type(exc).__name__,  # str(exc) is empty for timeouts
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            except ValueError as exc:
                # SDK bug anthropic-sdk#1265: from_json(partial_mode=True)
                # in the non-beta streaming path raises on malformed tool
                # input JSON instead of surfacing the buffer.
                if attempt >= 2:
                    raise
                last_exc = exc
                delay = _BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "anthropic.stream.json_retry error=%s attempt=%d delay_s=%.1f",
                    exc,
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
        component: str = "orchestrator",
        extra: dict[str, Any] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Message:
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
