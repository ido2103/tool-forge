"""Lifecycle hooks — priority-ordered, exception-swallowing dispatch.

Trimmed port of Zeemon ``core/hooks.py``: only the events the loop actually
fires, and stdlib ``logging`` instead of ``structlog``. Hooks are the loop's
observation seam — the REPL uses them to print one-liners, and the evals
subsystem will later attach here to record reuse/composition metrics. A handler
that raises is logged and skipped; it never aborts a turn.
"""

from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from collections.abc import Callable
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[..., Any]


class HookEvent(StrEnum):
    ON_ITERATION = "on_iteration"
    ON_TOOL_PRE_EXECUTE = "on_tool_pre_execute"
    ON_TOOL_POST_EXECUTE = "on_tool_post_execute"
    ON_INTERMEDIATE_TEXT = "on_intermediate_text"
    ON_RESPONSE = "on_response"
    # Forge build progress (fired by forge/tools.py and forge/worker.py, not the
    # loop): kwargs are `tool` and `phase` — "authoring_tests" / "tests_ready"
    # (+test_count) / "building" / "attempt" (+attempt, max_attempts) /
    # "verifying" / "attempt_failed" (+tampered) / "candidate_ready" (+attempts)
    # / "failed". A build is minutes long; without these it is dead air.
    ON_FORGE_PHASE = "on_forge_phase"


class HookManager:
    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[tuple[int, Handler]]] = defaultdict(list)

    def register(self, event: HookEvent, handler: Handler, *, priority: int = 50) -> None:
        """Register *handler* for *event*. Higher priority fires first."""
        bucket = self._handlers[event]
        bucket.append((priority, handler))
        bucket.sort(key=lambda t: t[0], reverse=True)

    async def fire(self, event: HookEvent, **kwargs: Any) -> list[Any]:
        """Fire every handler for *event*; collect results, swallow exceptions.

        Sync and async handlers are both supported (an awaitable return is
        awaited). A raising handler is logged at WARNING and skipped.
        """
        results: list[Any] = []
        for _priority, handler in self._handlers.get(event, []):
            try:
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                results.append(result)
            except Exception:
                logger.warning(
                    "hook handler error: event=%s handler=%s",
                    event.value,
                    getattr(handler, "__name__", repr(handler)),
                    exc_info=True,
                )
        return results
