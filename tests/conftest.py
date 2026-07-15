"""Shared fixtures for the toolforge test suite.

Settings fixtures pass every field explicitly (init kwargs outrank env vars in
pydantic-settings) and set ``_env_file=None``, so unit tests never read the
developer's ``.env`` or environment.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from toolforge.config import AnthropicSettings, WorkerSettings
from toolforge.providers import (
    AnthropicClient,
    Message,
    OpenAICompatClient,
    TextBlock,
    UsageEvent,
)
from toolforge.providers.usage import UsageHook

# ── env isolation ────────────────────────────────────────────────────────────


@pytest.fixture
def clean_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Scrub provider env vars and chdir away from any repo-root ``.env``."""
    for var in list(os.environ):
        if var.startswith("TOOLFORGE_") or var == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(var)
    monkeypatch.chdir(tmp_path)  # env_file=".env" is CWD-relative


# ── settings ─────────────────────────────────────────────────────────────────


@pytest.fixture
def anthropic_settings(tmp_path: Path) -> AnthropicSettings:
    return AnthropicSettings(
        _env_file=None,
        auth_mode="api_key",
        api_key=SecretStr("test-key"),
        oauth_credentials_path=tmp_path / "unused.json",
        model="claude-test",
        base_url=None,
        cache_ttl="ephemeral",
        extended_thinking="adaptive",
    )


@pytest.fixture
def oauth_creds_file(tmp_path: Path) -> Path:
    creds = tmp_path / "anthropic_oauth.json"
    creds.write_text(
        json.dumps(
            {
                "accessToken": "tok-fresh",
                "refreshToken": "ref-1",
                "expiresAt": int(time.time() * 1000) + 3_600_000,
            }
        )
    )
    creds.chmod(0o600)
    return creds


@pytest.fixture
def anthropic_settings_oauth(oauth_creds_file: Path) -> AnthropicSettings:
    return AnthropicSettings(
        _env_file=None,
        auth_mode="oauth",
        api_key=None,
        oauth_credentials_path=oauth_creds_file,
        model="claude-test",
        base_url=None,
        cache_ttl="ephemeral",
        extended_thinking="off",
    )


@pytest.fixture
def worker_settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        host="127.0.0.1",
        port=9999,
        model="test-model",
        api_key=SecretStr("EMPTY"),
    )


# ── usage hook recording ─────────────────────────────────────────────────────


@pytest.fixture
def recorded_usage() -> list[UsageEvent]:
    return []


@pytest.fixture
def usage_hook(recorded_usage: list[UsageEvent]) -> UsageHook:
    async def hook(event: UsageEvent) -> None:
        recorded_usage.append(event)

    return hook


# ── clients ──────────────────────────────────────────────────────────────────


@pytest.fixture
def anthropic_client(
    anthropic_settings: AnthropicSettings, usage_hook: UsageHook
) -> AnthropicClient:
    return AnthropicClient(anthropic_settings, usage_hook=usage_hook)


@pytest.fixture
def anthropic_client_oauth(
    anthropic_settings_oauth: AnthropicSettings, usage_hook: UsageHook
) -> AnthropicClient:
    return AnthropicClient(anthropic_settings_oauth, usage_hook=usage_hook)


@pytest.fixture
def worker_client(worker_settings: WorkerSettings, usage_hook: UsageHook) -> OpenAICompatClient:
    return OpenAICompatClient(worker_settings, usage_hook=usage_hook)


# ── canonical message factory ────────────────────────────────────────────────


@pytest.fixture
def user_msg() -> Callable[[str], Message]:
    def make(text: str = "hello") -> Message:
        return Message(role="user", content=[TextBlock(text=text)], ts=datetime.now(tz=UTC))

    return make


# ── SSE body builders ────────────────────────────────────────────────────────


@pytest.fixture
def anthropic_sse() -> Callable[[list[dict[str, Any]]], bytes]:
    """Anthropic Messages SSE body: ``event: <type>\\ndata: <json>\\n\\n`` per event."""

    def build(events: list[dict[str, Any]]) -> bytes:
        return b"".join(
            f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode() for ev in events
        )

    return build


@pytest.fixture
def chat_sse() -> Callable[[list[dict[str, Any]]], bytes]:
    """Chat Completions SSE body: ``data: <json>\\n\\n`` per chunk + ``[DONE]``."""

    def build(chunks: list[dict[str, Any]]) -> bytes:
        body = b"".join(f"data: {json.dumps(ch)}\n\n".encode() for ch in chunks)
        return body + b"data: [DONE]\n\n"

    return build


@pytest.fixture
def simple_anthropic_events() -> Callable[..., list[dict[str, Any]]]:
    """Standard Anthropic event list for a plain-text streamed response."""

    def build(
        *,
        text: str = "Hello world",
        stop_reason: str = "end_turn",
        input_tokens: int = 10,
        output_tokens: int = 12,
        model: str = "claude-test",
    ) -> list[dict[str, Any]]:
        half = len(text) // 2
        return [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text[:half]},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text[half:]},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
            {"type": "message_stop"},
        ]

    return build
