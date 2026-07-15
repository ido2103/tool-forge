"""Translation tests: canonical ↔ Anthropic wire format."""

from __future__ import annotations

from datetime import UTC, datetime

from toolforge.providers import (
    DocumentBlock,
    ImageBlock,
    Message,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from toolforge.providers.anthropic import anthropic_to_canonical, canonical_to_anthropic

_TS = datetime.now(tz=UTC)


def test_canonical_to_anthropic_block_shapes() -> None:
    messages = [
        Message(
            role="assistant",
            content=[
                ThinkingBlock(thinking="pondering", signature="sig"),
                RedactedThinkingBlock(data="blob"),
                TextBlock(text="Hello"),
                ToolUseBlock(id="toolu_1", name="get_weather", input={"city": "Paris"}),
            ],
            ts=_TS,
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="toolu_1", content="sunny"),
                DocumentBlock(title="notes", content="body"),
                ImageBlock(data="aGk=", media_type="image/png"),
            ],
            ts=_TS,
        ),
    ]
    out = canonical_to_anthropic(messages)
    assert out == [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "pondering", "signature": "sig"},
                {"type": "redacted_thinking", "data": "blob"},
                {"type": "text", "text": "Hello"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "sunny",
                    "is_error": False,
                },
                {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": "body"},
                    "title": "notes",
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
                },
            ],
        },
    ]


def test_whitespace_only_text_dropped() -> None:
    messages = [
        Message(role="user", content=[TextBlock(text="   "), TextBlock(text="real")], ts=_TS)
    ]
    out = canonical_to_anthropic(messages)
    assert out[0]["content"] == [{"type": "text", "text": "real"}]


def test_unsigned_thinking_dropped_entirely() -> None:
    """An unsigned ThinkingBlock survives block translation but the sanitizer strips it."""
    messages = [
        Message(
            role="assistant",
            content=[
                ThinkingBlock(thinking="local-model musings", signature=None),
                TextBlock(text="answer"),
            ],
            ts=_TS,
        )
    ]
    out = canonical_to_anthropic(messages)
    assert out[0]["content"] == [{"type": "text", "text": "answer"}]


def test_orphaned_tool_use_repaired() -> None:
    messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="toolu_orphan", name="t", input={})],
            ts=_TS,
        ),
        Message(role="user", content=[TextBlock(text="unrelated")], ts=_TS),
    ]
    out = canonical_to_anthropic(messages)
    first_user_block = out[1]["content"][0]
    assert first_user_block["type"] == "tool_result"
    assert first_user_block["tool_use_id"] == "toolu_orphan"
    assert first_user_block["is_error"] is True


def test_anthropic_to_canonical_blocks_and_usage() -> None:
    raw = {
        "role": "assistant",
        "model": "claude-test",
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "toolu_9", "name": "t", "input": {"a": 1}},
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "redacted_thinking", "data": "blob"},
            {"type": "mystery_block", "stuff": True},
        ],
        "usage": {
            "input_tokens": 7,
            "output_tokens": 11,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 4,
        },
    }
    msg = anthropic_to_canonical(raw, ts=_TS, auth_mode="api_key", latency_ms=5)
    assert msg.role == "assistant"
    assert msg.provider == "anthropic"
    assert msg.text == "hi[unhandled anthropic block: 'mystery_block']"
    assert msg.tool_use_blocks[0].input == {"a": 1}
    assert isinstance(msg.content[2], ThinkingBlock)
    assert isinstance(msg.content[3], RedactedThinkingBlock)
    assert msg.usage is not None
    assert msg.usage.cache_creation_input_tokens == 3
    assert msg.usage.cache_read_input_tokens == 4
    assert msg.stop_reason == "end_turn"
    assert msg.latency_ms == 5
