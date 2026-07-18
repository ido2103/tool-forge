"""Hook-manager tests — priority ordering, sync/async, exception isolation."""

from __future__ import annotations

from typing import Any

from toolforge.orchestrator.hooks import HookEvent, HookManager


async def test_sync_and_async_handlers_both_fire() -> None:
    hm = HookManager()
    seen: list[str] = []

    def sync_handler(**kwargs: Any) -> None:
        seen.append("sync")

    async def async_handler(**kwargs: Any) -> None:
        seen.append("async")

    hm.register(HookEvent.ON_RESPONSE, sync_handler)
    hm.register(HookEvent.ON_RESPONSE, async_handler)
    await hm.fire(HookEvent.ON_RESPONSE, text="hi")
    assert set(seen) == {"sync", "async"}


async def test_handler_exception_is_swallowed_others_still_fire() -> None:
    hm = HookManager()
    seen: list[str] = []

    def boom(**kwargs: Any) -> None:
        raise RuntimeError("handler bug")

    def ok(**kwargs: Any) -> None:
        seen.append("ok")

    hm.register(HookEvent.ON_ITERATION, boom)
    hm.register(HookEvent.ON_ITERATION, ok)
    results = await hm.fire(HookEvent.ON_ITERATION, iteration=1)
    assert seen == ["ok"]
    assert results == [None]  # only the surviving handler's return is collected


async def test_priority_ordering() -> None:
    hm = HookManager()
    order: list[str] = []

    hm.register(HookEvent.ON_RESPONSE, lambda **k: order.append("low"), priority=10)
    hm.register(HookEvent.ON_RESPONSE, lambda **k: order.append("high"), priority=90)
    await hm.fire(HookEvent.ON_RESPONSE, text="x")
    assert order == ["high", "low"]


async def test_fire_unregistered_event_is_noop() -> None:
    hm = HookManager()
    assert await hm.fire(HookEvent.ON_TOOL_PRE_EXECUTE) == []
