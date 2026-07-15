"""Sanitizer tests — ported from Zeemon's suite, minus the image-shrinking cases."""

from __future__ import annotations

from typing import Any

from toolforge.providers._anthropic_sanitize import (
    fix_orphaned_tool_uses,
    sanitize_messages_for_claude,
)


def test_unsigned_thinking_stripped() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "thinking", "thinking": "hmm", "signature": ""},
                {"type": "thinking", "thinking": "keep", "signature": "sig"},
                {"type": "text", "text": "hi"},
            ],
        }
    ]
    out = sanitize_messages_for_claude(messages)
    assert out[0]["content"] == [
        {"type": "thinking", "thinking": "keep", "signature": "sig"},
        {"type": "text", "text": "hi"},
    ]


def test_foreign_keys_stripped() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "t",
                    "input": {},
                    "_gemini_thought_signature": "xyz",
                }
            ],
        }
    ]
    out = sanitize_messages_for_claude(messages)
    assert out[0]["content"] == [{"type": "tool_use", "id": "toolu_1", "name": "t", "input": {}}]


def test_fully_emptied_assistant_patched() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "unsigned"}]}
    ]
    out = sanitize_messages_for_claude(messages)
    assert out[0]["content"] == [{"type": "text", "text": "..."}]


def test_fully_emptied_user_not_patched() -> None:
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": " "}]}]
    out = sanitize_messages_for_claude(messages)
    assert out[0]["content"] == []


def test_noop_returns_equal_messages() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": "plain string content"},
    ]
    out = sanitize_messages_for_claude(messages)
    assert out == messages


def test_orphan_prepended_to_next_user_message() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_a", "name": "t", "input": {}}],
        },
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
    ]
    assert fix_orphaned_tool_uses(messages) is True
    assert messages[1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_a",
        "content": "[Error: tool execution was interrupted]",
        "is_error": True,
    }
    assert messages[1]["content"][1] == {"type": "text", "text": "next"}


def test_orphan_inserts_new_user_message_at_end() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_b", "name": "t", "input": {}}],
        },
    ]
    assert fix_orphaned_tool_uses(messages) is True
    assert len(messages) == 2
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0]["tool_use_id"] == "toolu_b"


def test_fulfilled_tool_use_untouched() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_c", "name": "t", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_c", "content": "ok"}],
        },
    ]
    snapshot = [dict(m) for m in messages]
    assert fix_orphaned_tool_uses(messages) is False
    assert messages == snapshot
