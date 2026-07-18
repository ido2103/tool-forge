"""CandidateStore tests — replace-on-reforge semantics and lookup."""

from __future__ import annotations

import pytest

from toolforge.forge import Candidate, CandidateStore


def _candidate(name: str = "fetch_rss", behavior: str = "fetches a feed") -> Candidate:
    return Candidate(
        name=name,
        description="Fetch an RSS feed and return its entries as text.",
        input_schema={"type": "object", "properties": {}, "required": []},
        behavior=behavior,
        gap_analysis="no existing tool speaks RSS",
    )


def test_put_get_roundtrip() -> None:
    store = CandidateStore()
    candidate = _candidate()
    store.put(candidate)
    assert store.has("fetch_rss")
    assert store.get("fetch_rss") is candidate


def test_get_missing_returns_none() -> None:
    store = CandidateStore()
    assert store.get("nope") is None
    assert not store.has("nope")


def test_put_replaces_existing() -> None:
    # Re-forging a name is the revision path after a failed verification.
    store = CandidateStore()
    store.put(_candidate(behavior="v1"))
    revised = _candidate(behavior="v2")
    store.put(revised)
    assert store.get("fetch_rss") is revised


def test_pop_removes() -> None:
    store = CandidateStore()
    store.put(_candidate())
    popped = store.pop("fetch_rss")
    assert popped.name == "fetch_rss"
    assert not store.has("fetch_rss")


def test_pop_missing_raises() -> None:
    with pytest.raises(KeyError):
        CandidateStore().pop("nope")
