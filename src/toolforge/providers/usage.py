"""Per-call usage reporting — the pluggable replacement for Zeemon's cost ledger.

Zeemon's clients wrote token counts straight into Postgres via
``observability.cost.record_cost``. Toolforge has no database; instead each
client emits a :class:`UsageEvent` through an async :data:`UsageHook` at the end
of every model turn. The default hook logs the event; the evals subsystem will
later supply a hook that persists these for the README graphs.

Semantics change vs Zeemon (deliberate): clients invoke the hook inside a
``try/except`` — a broken usage hook must never abort a model turn.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class UsageEvent(BaseModel):
    """Token usage and metadata for one completed model turn."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    auth_mode: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    latency_ms: int
    stop_reason: str | None
    component: str
    turn_id: UUID | None = None


UsageHook = Callable[[UsageEvent], Awaitable[None]]


async def log_usage(event: UsageEvent) -> None:
    """Default usage hook: structured line via stdlib logging."""
    logger.info("provider.usage %s", event.model_dump_json())
