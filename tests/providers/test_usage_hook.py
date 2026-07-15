"""Usage hook tests."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from toolforge.providers import UsageEvent, log_usage


def _event() -> UsageEvent:
    return UsageEvent(
        provider="anthropic",
        auth_mode="api_key",
        model="claude-test",
        input_tokens=10,
        output_tokens=20,
        latency_ms=5,
        stop_reason="end_turn",
        component="orchestrator",
    )


async def test_log_usage_emits_structured_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="toolforge.providers.usage"):
        await log_usage(_event())
    assert "provider.usage" in caplog.text
    assert '"model":"claude-test"' in caplog.text
    assert '"input_tokens":10' in caplog.text


def test_usage_event_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        UsageEvent.model_validate({**_event().model_dump(), "surprise": 1})


def test_usage_event_cache_defaults() -> None:
    ev = _event()
    assert ev.cache_read_tokens == 0
    assert ev.cache_creation_tokens == 0
    assert ev.turn_id is None
