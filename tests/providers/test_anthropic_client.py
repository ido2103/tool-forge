"""AnthropicClient tests — respx-mocked SSE streams, both auth modes."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anthropic
import httpx
import pytest
import respx

import toolforge.providers.anthropic as anthropic_mod
from toolforge.config import AnthropicSettings
from toolforge.providers import (
    AnthropicClient,
    Message,
    MessageEnd,
    ProviderClient,
    TextDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
    UsageEvent,
)

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"

SseEvents = list[dict[str, Any]]


def _sse_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def _error_response(status: int, error_type: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "error", "error": {"type": error_type, "message": "boom"}},
    )


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic_mod, "_BASE_DELAY", 0.0)
    monkeypatch.setattr(anthropic_mod, "random", SimpleNamespace(uniform=lambda a, b: 0.0))


async def test_stream_text_happy_path(
    respx_mock: respx.MockRouter,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=_sse_response(anthropic_sse(simple_anthropic_events()))
    )

    events = [
        ev
        async for ev in anthropic_client.stream(
            messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
        )
    ]

    text_deltas = [ev for ev in events if isinstance(ev, TextDelta)]
    assert "".join(d.text for d in text_deltas) == "Hello world"
    end = events[-1]
    assert isinstance(end, MessageEnd)
    msg = end.message
    assert msg.text == "Hello world"
    assert msg.stop_reason == "end_turn"
    assert msg.provider == "anthropic"
    assert msg.auth_mode == "api_key"
    assert msg.usage is not None
    assert (msg.usage.input_tokens, msg.usage.output_tokens) == (10, 12)
    assert msg.latency_ms is not None

    req = respx_mock.calls[0].request
    assert req.headers["x-api-key"] == "test-key"
    body = json.loads(req.content)
    assert body["system"] == [{"type": "text", "text": "sys"}]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["stream"] is True
    assert "tools" not in body


async def test_cache_ttl_1h_and_thinking_off(
    respx_mock: respx.MockRouter,
    anthropic_settings: AnthropicSettings,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    settings = anthropic_settings.model_copy(update={"cache_ttl": "1h", "extended_thinking": "off"})
    client = AnthropicClient(settings)
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=_sse_response(anthropic_sse(simple_anthropic_events()))
    )

    await client.send(messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64)

    body = json.loads(respx_mock.calls[0].request.content)
    assert body["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "thinking" not in body


async def test_tool_use_stream(
    respx_mock: respx.MockRouter,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
) -> None:
    events_in: SseEvents = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "get_weather",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"city": '},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    ]
    respx_mock.post(_MESSAGES_URL).mock(return_value=_sse_response(anthropic_sse(events_in)))

    tools = [
        {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    events = [
        ev
        async for ev in anthropic_client.stream(
            messages=[user_msg("weather?")],
            system="sys",
            model="claude-test",
            max_tokens=64,
            tools=tools,
        )
    ]

    starts = [ev for ev in events if isinstance(ev, ToolUseStart)]
    deltas = [ev for ev in events if isinstance(ev, ToolUseDelta)]
    ends = [ev for ev in events if isinstance(ev, ToolUseEnd)]
    assert starts == [ToolUseStart(id="toolu_01", name="get_weather")]
    assert "".join(d.partial_json for d in deltas) == '{"city": "Paris"}'
    assert ends == [ToolUseEnd(id="toolu_01")]

    end = events[-1]
    assert isinstance(end, MessageEnd)
    assert end.message.stop_reason == "tool_use"
    assert end.message.tool_use_blocks[0].input == {"city": "Paris"}

    body = json.loads(respx_mock.calls[0].request.content)
    assert body["tools"] == tools  # Anthropic-shape tools pass through untouched


async def test_send_fires_text_callback_and_swallows_its_errors(
    respx_mock: respx.MockRouter,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=_sse_response(anthropic_sse(simple_anthropic_events()))
    )
    chunks: list[str] = []

    async def on_text(t: str) -> None:
        chunks.append(t)
        raise RuntimeError("callback bug")  # must not abort the request

    msg = await anthropic_client.send(
        messages=[user_msg("hi")],
        system="sys",
        model="claude-test",
        max_tokens=64,
        on_text_delta=on_text,
    )
    assert msg.text == "Hello world"
    assert "".join(chunks) == "Hello world"


async def test_retry_on_529_then_success(
    respx_mock: respx.MockRouter,
    no_retry_delay: None,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    route = respx_mock.post(_MESSAGES_URL)
    route.side_effect = [
        _error_response(529, "overloaded_error"),
        _sse_response(anthropic_sse(simple_anthropic_events())),
    ]

    msg = await anthropic_client.send(
        messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
    )
    assert msg.text == "Hello world"
    assert route.call_count == 2


async def test_retry_exhaustion_reraises(
    respx_mock: respx.MockRouter,
    no_retry_delay: None,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
) -> None:
    route = respx_mock.post(_MESSAGES_URL).mock(
        return_value=_error_response(529, "overloaded_error")
    )

    with pytest.raises(anthropic.APIStatusError):
        await anthropic_client.send(
            messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
        )
    assert route.call_count == anthropic_mod._MAX_STREAM_RETRIES + 1


async def test_non_retryable_400_raises_immediately(
    respx_mock: respx.MockRouter,
    no_retry_delay: None,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
) -> None:
    route = respx_mock.post(_MESSAGES_URL).mock(
        return_value=_error_response(400, "invalid_request_error")
    )

    with pytest.raises(anthropic.BadRequestError):
        await anthropic_client.send(
            messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
        )
    assert route.call_count == 1


async def test_timeout_retried(
    respx_mock: respx.MockRouter,
    no_retry_delay: None,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    route = respx_mock.post(_MESSAGES_URL)
    route.side_effect = [
        httpx.ReadTimeout("timed out"),
        _sse_response(anthropic_sse(simple_anthropic_events())),
    ]

    msg = await anthropic_client.send(
        messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
    )
    assert msg.text == "Hello world"
    assert route.call_count == 2


async def test_oauth_masquerade_headers_and_system_prefix(
    respx_mock: respx.MockRouter,
    anthropic_client_oauth: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=_sse_response(anthropic_sse(simple_anthropic_events()))
    )

    await anthropic_client_oauth.send(
        messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
    )

    req = respx_mock.calls[0].request
    assert req.headers["authorization"] == "Bearer tok-fresh"
    assert "x-api-key" not in req.headers
    for beta in (
        "interleaved-thinking-2025-05-14",
        "fine-grained-tool-streaming-2025-05-14",
        "claude-code-20250219",
        "oauth-2025-04-20",
    ):
        assert beta in req.headers["anthropic-beta"]
    # httpx merges the SDK's own UA with ours: "AsyncAnthropic/..., claude-cli/..."
    assert "claude-cli/" in req.headers["user-agent"]
    assert req.headers["x-app"] == "cli"

    body = json.loads(req.content)
    assert body["system"] == [
        {"type": "text", "text": anthropic_mod._CLAUDE_CODE_SYSTEM_PREFIX},
        {"type": "text", "text": "sys"},
    ]


async def test_oauth_401_refreshes_token_and_retries(
    respx_mock: respx.MockRouter,
    anthropic_client_oauth: AnthropicClient,
    oauth_creds_file: Path,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    messages_route = respx_mock.post(_MESSAGES_URL)
    messages_route.side_effect = [
        _error_response(401, "authentication_error"),
        _sse_response(anthropic_sse(simple_anthropic_events())),
    ]
    respx_mock.post(_REFRESH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "tok-new", "refresh_token": "ref-2", "expires_in": 3600},
        )
    )

    msg = await anthropic_client_oauth.send(
        messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
    )
    assert msg.text == "Hello world"
    assert messages_route.call_count == 2
    assert messages_route.calls[1].request.headers["authorization"] == "Bearer tok-new"

    creds = json.loads(oauth_creds_file.read_text())
    assert creds["accessToken"] == "tok-new"
    assert creds["refreshToken"] == "ref-2"
    assert creds["expiresAt"] > int(time.time() * 1000)


async def test_usage_hook_receives_event(
    respx_mock: respx.MockRouter,
    anthropic_client: AnthropicClient,
    recorded_usage: list[UsageEvent],
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=_sse_response(anthropic_sse(simple_anthropic_events()))
    )
    turn = uuid4()

    await anthropic_client.send(
        messages=[user_msg("hi")],
        system="sys",
        model="claude-test",
        max_tokens=64,
        component="tester",
        turn_id=turn,
    )

    assert len(recorded_usage) == 1
    ev = recorded_usage[0]
    assert ev.provider == "anthropic"
    assert ev.auth_mode == "api_key"
    assert ev.model == "claude-test"
    assert (ev.input_tokens, ev.output_tokens) == (10, 12)
    assert (ev.cache_read_tokens, ev.cache_creation_tokens) == (0, 0)
    assert ev.stop_reason == "end_turn"
    assert ev.component == "tester"
    assert ev.turn_id == turn
    assert ev.latency_ms >= 0


async def test_raising_usage_hook_does_not_break_stream(
    respx_mock: respx.MockRouter,
    anthropic_settings: AnthropicSettings,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    async def broken_hook(event: UsageEvent) -> None:
        raise RuntimeError("hook exploded")

    client = AnthropicClient(anthropic_settings, usage_hook=broken_hook)
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=_sse_response(anthropic_sse(simple_anthropic_events()))
    )

    msg = await client.send(
        messages=[user_msg("hi")], system="sys", model="claude-test", max_tokens=64
    )
    assert msg.text == "Hello world"


async def test_cancel_event_aborts_stream(
    respx_mock: respx.MockRouter,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[[SseEvents], bytes],
    simple_anthropic_events: Callable[..., SseEvents],
) -> None:
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=_sse_response(anthropic_sse(simple_anthropic_events()))
    )
    cancel = asyncio.Event()
    cancel.set()

    with pytest.raises(asyncio.CancelledError):
        await anthropic_client.send(
            messages=[user_msg("hi")],
            system="sys",
            model="claude-test",
            max_tokens=64,
            cancel_event=cancel,
        )


def test_protocol_conformance(anthropic_client: AnthropicClient) -> None:
    client: ProviderClient = anthropic_client  # mypy: structural conformance, no cast
    assert client.name == "anthropic"
