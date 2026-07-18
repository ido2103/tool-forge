"""Forge: turns a tool spec into a verified, registered tool.

Orchestrator-facing surface (implemented as stubs): two composable tools.
``forge_tool`` takes a spec (gap analysis, name, description, input schema,
behavior contract) and builds a *candidate* — code + tests in the sandbox —
without registering anything. ``register_tool`` promotes a candidate into the
live registry, but only after the orchestrator has independently verified it
(holdout inputs via run_bash and/or code review): green tests alone never
register a tool.

Internal loop (future slice): a frontier model writes adversarial tests from
the spec alone (TDD, before any implementation), then the forge worker (api or
local backend, never the orchestrator's model) implements against them in a
harness until green, with a docs-RAG tool for real API documentation and a
bounded iteration budget that escalates failures back to the orchestrator.
"""

from toolforge.forge.candidates import Candidate, CandidateStore
from toolforge.forge.tools import build_forge_tool, build_register_tool

__all__ = [
    "Candidate",
    "CandidateStore",
    "build_forge_tool",
    "build_register_tool",
]
