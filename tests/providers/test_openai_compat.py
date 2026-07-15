"""OpenAICompatClient tests — Chat Completions translation + respx-mocked streaming."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

import toolforge.providers.openai_compat as compat_mod
from toolforge.providers import (
    Message,
    MessageEnd,
    OpenAICompatClient,
    ProviderClient,
    TextBlock,
    ThinkingBlock,
    ThinkingDelta,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
    UsageEvent,
)
from toolforge.providers.openai_compat import (
    IdMapper,
    anthropic_tools_to_openai,
    canonical_to_chat_messages,
)

_CHAT_URL = "http://127.0.0.1:9999/v1/chat/completions"
_TS = datetime.now(tz=UTC)

Chunks = list[dict[str, Any]]


def _chunk(
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [],
    }
    if delta is not None or finish is not None:
        out["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    if usage is not None:
        out["usage"] = usage
    return out


def _text_chunks(text: str = "Hello world", finish: str = "stop") -> Chunks:
    half = len(text) // 2
    return [
        _chunk(delta={"role": "assistant", "content": text[:half]}),
        _chunk(delta={"content": text[half:]}),
        _chunk(finish=finish),
        _chunk(usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}),
    ]


def _sse_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat_mod, "_BASE_DELAY", 0.0)
    monkeypatch.setattr(compat_mod, "random", SimpleNamespace(uniform=lambda a, b: 0.0))


# ── translation ──────────────────────────────────────────────────────────────


def test_translation_system_first_and_plain_user_text() -> None:
    messages = [Message(role="user", content=[TextBlock(text="hi")], ts=_TS)]
    out = canonical_to_chat_messages(messages, system="sys", id_mapper=IdMapper())
    assert out == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_translation_tool_flow_ids_consistent() -> None:
    mapper = IdMapper()
    messages = [
        Message(role="user", content=[TextBlock(text="weather?")], ts=_TS),
        Message(
            role="assistant",
            content=[
                TextBlock(text="checking"),
                ToolUseBlock(id="toolu_x", name="get_weather", input={"city": "Paris"}),
            ],
            ts=_TS,
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="toolu_x", content="sunny")],
            ts=_TS,
        ),
    ]
    out = canonical_to_chat_messages(messages, system="sys", id_mapper=mapper)

    assistant = out[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "checking"
    (tool_call,) = assistant["tool_calls"]
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "get_weather"
    assert json.loads(tool_call["function"]["arguments"]) == {"city": "Paris"}
    assert tool_call["id"].startswith("call_")

    tool_msg = out[3]
    assert tool_msg == {
        "role": "tool",
        "tool_call_id": tool_call["id"],  # same minted id on both sides
        "content": "sunny",
    }


def test_translation_drops_thinking_outbound() -> None:
    messages = [
        Message(
            role="assistant",
            content=[ThinkingBlock(thinking="hmm", signature=None), TextBlock(text="answer")],
            ts=_TS,
        )
    ]
    out = canonical_to_chat_messages(messages, system="sys", id_mapper=IdMapper())
    assert out[1] == {"role": "assistant", "content": "answer"}


def test_anthropic_tools_to_openai_shape() -> None:
    tools = [
        {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    assert anthropic_tools_to_openai(tools) == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]


def test_id_mapper_mints_stable_bidirectional_ids() -> None:
    mapper = IdMapper()
    canon = mapper.mint_for_openai("call_1")
    assert canon.startswith("toolu_")
    assert mapper.mint_for_openai("call_1") == canon
    assert mapper.canonical_to_openai(canon) == "call_1"
    assert mapper.canonical_to_openai("toolu_unknown") is None


# ── streaming ────────────────────────────────────────────────────────────────


async def test_stream_text_happy_path(
    respx_mock: respx.MockRouter,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    respx_mock.post(_CHAT_URL).mock(return_value=_sse_response(chat_sse(_text_chunks())))

    events = [
        ev
        async for ev in worker_client.stream(
            messages=[user_msg("hi")], system="sys", model="test-model", max_tokens=128
        )
    ]

    text_deltas = [ev for ev in events if isinstance(ev, TextDelta)]
    assert "".join(d.text for d in text_deltas) == "Hello world"
    end = events[-1]
    assert isinstance(end, MessageEnd)
    msg = end.message
    assert msg.text == "Hello world"
    assert msg.stop_reason == "end_turn"  # mapped from "stop"
    assert msg.provider == "openai"
    assert msg.usage is not None
    assert (msg.usage.input_tokens, msg.usage.output_tokens) == (5, 7)

    body = json.loads(respx_mock.calls[0].request.content)
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 128
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0] == {"role": "system", "content": "sys"}


@pytest.mark.parametrize(
    ("finish", "expected"),
    [("length", "max_tokens"), ("content_filter", "refusal"), ("weird_reason", "weird_reason")],
)
async def test_finish_reason_mapping(
    respx_mock: respx.MockRouter,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
    finish: str,
    expected: str,
) -> None:
    respx_mock.post(_CHAT_URL).mock(
        return_value=_sse_response(chat_sse(_text_chunks(finish=finish)))
    )
    msg = await worker_client.send(
        messages=[user_msg("hi")], system="sys", model="test-model", max_tokens=128
    )
    assert msg.stop_reason == expected


async def test_stream_tool_calls(
    respx_mock: respx.MockRouter,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    chunks = [
        _chunk(
            delta={
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ],
            }
        ),
        _chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": '{"city": '}}]}),
        _chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
        _chunk(finish="tool_calls"),
        _chunk(usage={"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}),
    ]
    respx_mock.post(_CHAT_URL).mock(return_value=_sse_response(chat_sse(chunks)))

    tools = [{"name": "get_weather", "description": "d", "input_schema": {"type": "object"}}]
    events = [
        ev
        async for ev in worker_client.stream(
            messages=[user_msg("weather?")],
            system="sys",
            model="test-model",
            max_tokens=128,
            tools=tools,
        )
    ]

    starts = [ev for ev in events if isinstance(ev, ToolUseStart)]
    deltas = [ev for ev in events if isinstance(ev, ToolUseDelta)]
    ends = [ev for ev in events if isinstance(ev, ToolUseEnd)]
    assert len(starts) == 1
    assert starts[0].name == "get_weather"
    assert starts[0].id.startswith("toolu_")
    assert "".join(d.partial_json for d in deltas) == '{"city": "Paris"}'
    assert ends == [ToolUseEnd(id=starts[0].id)]

    end = events[-1]
    assert isinstance(end, MessageEnd)
    assert end.message.stop_reason == "tool_use"
    (tool_block,) = end.message.tool_use_blocks
    assert tool_block.id == starts[0].id
    assert tool_block.input == {"city": "Paris"}

    body = json.loads(respx_mock.calls[0].request.content)
    assert body["tools"] == anthropic_tools_to_openai(tools)


async def test_malformed_tool_args_fall_back_to_empty_input(
    respx_mock: respx.MockRouter,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    chunks = [
        _chunk(
            delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{not json"},
                    }
                ]
            }
        ),
        _chunk(finish="tool_calls"),
    ]
    respx_mock.post(_CHAT_URL).mock(return_value=_sse_response(chat_sse(chunks)))

    msg = await worker_client.send(
        messages=[user_msg("go")], system="sys", model="test-model", max_tokens=128
    )
    assert msg.tool_use_blocks[0].input == {}
    assert msg.stop_reason == "tool_use"


async def test_reasoning_content_becomes_thinking(
    respx_mock: respx.MockRouter,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    chunks = [
        _chunk(delta={"role": "assistant", "reasoning_content": "let me think"}),
        _chunk(delta={"content": "answer"}),
        _chunk(finish="stop"),
    ]
    respx_mock.post(_CHAT_URL).mock(return_value=_sse_response(chat_sse(chunks)))

    events = [
        ev
        async for ev in worker_client.stream(
            messages=[user_msg("hi")], system="sys", model="test-model", max_tokens=128
        )
    ]
    thinking_deltas = [ev for ev in events if isinstance(ev, ThinkingDelta)]
    assert [d.text for d in thinking_deltas] == ["let me think"]

    end = events[-1]
    assert isinstance(end, MessageEnd)
    thinking_block = end.message.content[0]
    assert isinstance(thinking_block, ThinkingBlock)
    assert thinking_block.thinking == "let me think"
    assert thinking_block.signature is None
    assert end.message.text == "answer"


async def test_stop_finish_with_tools_normalized_to_tool_use(
    respx_mock: respx.MockRouter,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    chunks = [
        _chunk(
            delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_z",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ]
            }
        ),
        _chunk(finish="stop"),  # llama.cpp quirk: "stop" despite tool calls
    ]
    respx_mock.post(_CHAT_URL).mock(return_value=_sse_response(chat_sse(chunks)))

    msg = await worker_client.send(
        messages=[user_msg("go")], system="sys", model="test-model", max_tokens=128
    )
    assert msg.stop_reason == "tool_use"


async def test_usage_hook_receives_worker_event(
    respx_mock: respx.MockRouter,
    worker_client: OpenAICompatClient,
    recorded_usage: list[UsageEvent],
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    respx_mock.post(_CHAT_URL).mock(return_value=_sse_response(chat_sse(_text_chunks())))

    await worker_client.send(
        messages=[user_msg("hi")], system="sys", model="test-model", max_tokens=128
    )

    assert len(recorded_usage) == 1
    ev = recorded_usage[0]
    assert ev.provider == "openai"
    assert ev.auth_mode == "api_key"
    assert ev.component == "forge_worker"
    assert (ev.input_tokens, ev.output_tokens) == (5, 7)
    assert ev.stop_reason == "end_turn"


async def test_retry_on_503_then_success(
    respx_mock: respx.MockRouter,
    no_retry_delay: None,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    route = respx_mock.post(_CHAT_URL)
    route.side_effect = [
        httpx.Response(503, json={"error": {"message": "warming up"}}),
        _sse_response(chat_sse(_text_chunks())),
    ]

    msg = await worker_client.send(
        messages=[user_msg("hi")], system="sys", model="test-model", max_tokens=128
    )
    assert msg.text == "Hello world"
    assert route.call_count == 2


async def test_connect_error_retried(
    respx_mock: respx.MockRouter,
    no_retry_delay: None,
    worker_client: OpenAICompatClient,
    user_msg: Callable[[str], Message],
    chat_sse: Callable[[Chunks], bytes],
) -> None:
    route = respx_mock.post(_CHAT_URL)
    route.side_effect = [
        httpx.ConnectError("connection refused"),
        _sse_response(chat_sse(_text_chunks())),
    ]

    msg = await worker_client.send(
        messages=[user_msg("hi")], system="sys", model="test-model", max_tokens=128
    )
    assert msg.text == "Hello world"
    assert route.call_count == 2


def test_protocol_conformance(worker_client: OpenAICompatClient) -> None:
    client: ProviderClient = worker_client  # mypy: structural conformance, no cast
    assert client.name == "openai"
