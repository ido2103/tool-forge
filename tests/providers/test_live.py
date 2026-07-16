"""Live smoke tests — hit real endpoints. Deselected by default (pytest.ini addopts).

Run manually:  uv run pytest -m live
Requires: Anthropic credentials (ANTHROPIC_API_KEY or OAuth creds per .env) for
the orchestrator test, and a running OpenAI-compatible server (vLLM/llama.cpp)
per TOOLFORGE_WORKER_* for the worker tests. Missing pieces skip, not fail.
Note: in OAuth mode a stale token is refreshed and the creds file rewritten.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from toolforge.config import AnthropicSettings, WorkerSettings
from toolforge.providers import AnthropicClient, Message, OpenAICompatClient, TextBlock

pytestmark = pytest.mark.live


def _user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)], ts=datetime.now(tz=UTC))


def _anthropic_settings_or_skip() -> AnthropicSettings:
    try:
        return AnthropicSettings()
    except ValidationError:
        pytest.skip("no Anthropic credentials configured (env vars / .env)")


def _worker_settings_or_skip() -> WorkerSettings:
    settings = WorkerSettings()
    try:
        httpx.get(f"{settings.base_url}/models", timeout=2.0).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"no worker server reachable at {settings.base_url}")
    return settings


async def test_anthropic_roundtrip() -> None:
    settings = _anthropic_settings_or_skip()
    client = AnthropicClient(settings)

    msg = await client.send(
        messages=[_user("Reply with exactly one word: pong")],
        system="You are a connectivity probe. Follow instructions exactly.",
        model=settings.model,
        max_tokens=4096,  # headroom for adaptive thinking
    )

    assert msg.stop_reason == "end_turn"
    assert msg.text.strip()
    assert msg.usage is not None
    assert msg.usage.output_tokens > 0


async def test_worker_roundtrip() -> None:
    settings = _worker_settings_or_skip()
    client = OpenAICompatClient(settings)

    msg = await client.send(
        messages=[_user("Reply with exactly one word: pong")],
        system="You are a connectivity probe. Follow instructions exactly.",
        model=settings.model,
        max_tokens=2048,  # headroom for reasoning models
    )

    # stop_reason must land in the normalized Anthropic vocabulary.
    assert msg.stop_reason in {"end_turn", "max_tokens"}
    assert msg.content, "worker returned an empty message"
    assert msg.usage is None or msg.usage.output_tokens > 0


@pytest.mark.xfail(strict=False, reason="tool-call support varies across local servers/models")
async def test_worker_tool_call() -> None:
    settings = _worker_settings_or_skip()
    client = OpenAICompatClient(settings)
    tools = [
        {
            "name": "echo",
            "description": "Echo the provided text back to the caller.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    ]

    msg = await client.send(
        messages=[_user("Call the echo tool with the text 'hi'. You must use the tool.")],
        system="You are a tool-use probe. Always use the provided tools.",
        model=settings.model,
        max_tokens=2048,
        tools=tools,
    )

    assert msg.stop_reason == "tool_use"
    assert msg.tool_use_blocks
    assert msg.tool_use_blocks[0].name == "echo"
