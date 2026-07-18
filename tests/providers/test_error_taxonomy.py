"""Provider error-taxonomy tests — SDK exceptions → neutral Transient/Permanent.

The classifier and per-adapter translators are unit-tested directly; the
CancelledError pass-through is an integration test through send().
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import anthropic
import httpx
import openai
import pytest
import respx

import toolforge.providers.anthropic as anthropic_mod
import toolforge.providers.openai_compat as openai_mod
from toolforge.providers import (
    AnthropicClient,
    Message,
    PermanentProviderError,
    TransientProviderError,
    is_transient_status,
)

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


# ── classifier ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "err_type", "expected"),
    [
        (429, None, True),
        (500, None, True),
        (502, None, True),
        (503, None, True),
        (529, None, True),
        (200, "api_error", True),  # mid-stream SSE error masquerading as HTTP 200
        (400, None, False),
        (401, None, False),
        (404, None, False),
        (200, None, False),
        (None, None, False),
    ],
)
def test_is_transient_status(status: int | None, err_type: str | None, expected: bool) -> None:
    assert is_transient_status(status, err_type) is expected


# ── anthropic translator ─────────────────────────────────────────────────────


def _anthropic_status_error(status: int, err_type: str) -> anthropic.APIStatusError:
    resp = httpx.Response(status, request=httpx.Request("POST", _MESSAGES_URL))
    return anthropic.APIStatusError("boom", response=resp, body={"error": {"type": err_type}})


def test_anthropic_429_is_transient() -> None:
    err = anthropic_mod._to_provider_error(_anthropic_status_error(429, "rate_limit_error"))
    assert isinstance(err, TransientProviderError)


def test_anthropic_400_is_permanent() -> None:
    err = anthropic_mod._to_provider_error(_anthropic_status_error(400, "invalid_request_error"))
    assert isinstance(err, PermanentProviderError)


def test_anthropic_200_api_error_is_transient() -> None:
    err = anthropic_mod._to_provider_error(_anthropic_status_error(200, "api_error"))
    assert isinstance(err, TransientProviderError)


def test_anthropic_connection_error_is_transient() -> None:
    exc = anthropic.APIConnectionError(request=httpx.Request("POST", _MESSAGES_URL))
    assert isinstance(anthropic_mod._to_provider_error(exc), TransientProviderError)


def test_anthropic_timeout_is_transient() -> None:
    exc = httpx.ReadTimeout("timed out")
    assert isinstance(anthropic_mod._to_provider_error(exc), TransientProviderError)


# ── openai translator ────────────────────────────────────────────────────────


def _openai_status_error(status: int, err_type: str) -> openai.APIStatusError:
    resp = httpx.Response(status, request=httpx.Request("POST", "http://localhost:8000/v1"))
    return openai.APIStatusError("boom", response=resp, body={"error": {"type": err_type}})


def test_openai_500_is_transient() -> None:
    err = openai_mod._to_provider_error(_openai_status_error(500, "server_error"))
    assert isinstance(err, TransientProviderError)


def test_openai_400_is_permanent() -> None:
    err = openai_mod._to_provider_error(_openai_status_error(400, "invalid_request_error"))
    assert isinstance(err, PermanentProviderError)


def test_openai_connection_error_is_transient() -> None:
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://localhost:8000/v1"))
    assert isinstance(openai_mod._to_provider_error(exc), TransientProviderError)


# ── CancelledError passes through send() untranslated ────────────────────────


async def test_cancelled_error_not_translated(
    respx_mock: respx.MockRouter,
    anthropic_client: AnthropicClient,
    user_msg: Callable[[str], Message],
    anthropic_sse: Callable[..., bytes],
    simple_anthropic_events: Callable[..., list[dict[str, object]]],
) -> None:
    respx_mock.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=anthropic_sse(simple_anthropic_events()),
        )
    )
    cancel = asyncio.Event()
    cancel.set()  # cooperative abort trips on the first streamed event

    with pytest.raises(asyncio.CancelledError):
        await anthropic_client.send(
            messages=[user_msg("hi")],
            system="sys",
            model="claude-test",
            max_tokens=64,
            cancel_event=cancel,
        )
