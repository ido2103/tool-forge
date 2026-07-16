"""Canonical message type tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from toolforge.providers import (
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


def _full_message() -> Message:
    return Message(
        role="assistant",
        content=[
            ThinkingBlock(thinking="pondering", signature="sig"),
            RedactedThinkingBlock(data="blob"),
            TextBlock(text="Hello "),
            TextBlock(text="world"),
            ToolUseBlock(id="toolu_1", name="get_weather", input={"city": "Paris"}),
            ToolResultBlock(tool_use_id="toolu_1", content="sunny", is_error=False),
            DocumentBlock(title="notes", content="body"),
            ImageBlock(data="aGk=", media_type="image/png"),
        ],
        ts=datetime.now(tz=UTC),
        provider="anthropic",
        auth_mode="api_key",
        model="claude-test",
        usage=Usage(input_tokens=10, output_tokens=20, cache_read_input_tokens=5),
        stop_reason="end_turn",
        latency_ms=42,
    )


def test_json_round_trip() -> None:
    msg = _full_message()
    dumped = msg.model_dump(mode="json")
    restored = Message.model_validate(dumped)
    assert restored == msg


def test_text_property_concatenates_text_blocks_only() -> None:
    msg = _full_message()
    assert msg.text == "Hello world"


def test_tool_use_blocks_filter() -> None:
    msg = _full_message()
    blocks = msg.tool_use_blocks
    assert len(blocks) == 1
    assert blocks[0].name == "get_weather"
    assert blocks[0].input == {"city": "Paris"}


def test_unknown_block_field_rejected() -> None:
    with pytest.raises(ValidationError):
        TextBlock.model_validate({"type": "text", "text": "x", "bogus": 1})


def test_unknown_block_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Message.model_validate(
            {
                "role": "user",
                "content": [{"type": "no_such_block"}],
                "ts": datetime.now(tz=UTC).isoformat(),
            }
        )
