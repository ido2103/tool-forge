"""Candidate storage — forged-but-not-yet-registered tools.

A candidate is the forge's output *before* the orchestrator's holdout check:
the spec it was built from plus (once the build loop exists) the code, tests,
and test report. Candidates live in memory only and die with the session —
v1 forges mid-task (pause/build/resume), so nothing needs to survive a restart.
Re-forging a name replaces the previous candidate: that is the revision path
after a failed verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """The orchestrator-authored contract a tool is forged from.

    Carries exactly what the build loop (test author, then worker) needs —
    ``gap_analysis`` is deliberately absent: it justifies *whether* to forge,
    not *what* to build, so it stays on :class:`Candidate` for provenance.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    behavior: str
    allowed_domains: tuple[str, ...] = ()
    examples: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_validated_input(cls, inp: dict[str, Any]) -> ToolSpec:
        """Build from a ``forge_tool`` input dict that already passed validation."""
        return cls(
            name=inp["name"],
            description=inp["description"],
            input_schema=inp["input_schema"],
            behavior=inp["behavior"],
            allowed_domains=tuple(inp.get("allowed_domains") or ()),
            examples=tuple(inp.get("examples") or ()),
        )


@dataclass
class Candidate:
    """A forged tool awaiting the orchestrator's holdout check and registration."""

    name: str
    description: str
    input_schema: dict[str, Any]
    behavior: str
    gap_analysis: str
    allowed_domains: list[str] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)
    # Build artifacts, populated by the forge's internal loop. Paths point into
    # the sandbox workspace so the orchestrator can exercise them via run_bash.
    code_path: str | None = None
    test_path: str | None = None
    test_report: str | None = None


class CandidateStore:
    """In-memory candidates keyed by tool name; ``put`` replaces unconditionally."""

    def __init__(self) -> None:
        self._candidates: dict[str, Candidate] = {}

    def put(self, candidate: Candidate) -> None:
        self._candidates[candidate.name] = candidate

    def get(self, name: str) -> Candidate | None:
        return self._candidates.get(name)

    def has(self, name: str) -> bool:
        return name in self._candidates

    def pop(self, name: str) -> Candidate:
        """Remove and return a candidate (the promotion path). ``KeyError`` if absent."""
        return self._candidates.pop(name)

    def clear(self) -> None:
        """Drop every candidate (the ``/reset`` path)."""
        self._candidates.clear()
