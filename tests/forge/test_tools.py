"""forge_tool / register_tool tests — schemas, validation, guided stub errors."""

from __future__ import annotations

from typing import Any

import pytest

from toolforge.config import SandboxSettings
from toolforge.forge import Candidate, CandidateStore, build_forge_tool, build_register_tool
from toolforge.registry import RegisteredTool, ToolContext, ToolRegistry, ToolResult
from toolforge.sandbox.bash import BashSandbox
from toolforge.sandbox.run_bash import build_run_bash

from tests.sandbox.test_bash import FakeRunner


def _build() -> tuple[RegisteredTool, RegisteredTool, CandidateStore, ToolRegistry]:
    store = CandidateStore()
    registry = ToolRegistry(ToolContext())
    forge = build_forge_tool(store, registry)
    register = build_register_tool(store, registry)
    return forge, register, store, registry


def _valid_input() -> dict[str, Any]:
    return {
        "gap_analysis": "run_bash cannot parse RSS without per-call boilerplate scripts",
        "name": "fetch_rss",
        "description": "Fetch an RSS feed URL and return its entries as titled text.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The feed URL."}},
            "required": ["url"],
        },
        "behavior": "Returns one line per entry; on HTTP failure returns an error string.",
    }


def _candidate(name: str = "fetch_rss") -> Candidate:
    spec = _valid_input()
    return Candidate(
        name=name,
        description=spec["description"],
        input_schema=spec["input_schema"],
        behavior=spec["behavior"],
        gap_analysis=spec["gap_analysis"],
    )


async def _call(tool: RegisteredTool, inp: dict[str, Any]) -> ToolResult:
    return await tool.handler(inp, ToolContext())


# ── schema shape ─────────────────────────────────────────────────────────────


def test_forge_schema_shape() -> None:
    forge, _, _, _ = _build()
    assert forge.name == "forge_tool"
    assert forge.trust == "TRUSTED"
    # gap_analysis first: generation order is reasoning order — the model must
    # commit to the reuse check before it starts designing the new tool.
    assert forge.input_schema["required"] == [
        "gap_analysis",
        "name",
        "description",
        "input_schema",
        "behavior",
    ]
    assert set(forge.input_schema["properties"]) == {
        "gap_analysis",
        "name",
        "description",
        "input_schema",
        "behavior",
        "allowed_domains",
        "examples",
    }


def test_register_schema_shape() -> None:
    _, register, _, _ = _build()
    assert register.name == "register_tool"
    assert register.trust == "TRUSTED"
    assert register.input_schema["required"] == ["holdout_evidence", "name"]


# ── forge_tool validation ────────────────────────────────────────────────────


async def test_forge_missing_gap_analysis_is_error() -> None:
    forge, _, _, _ = _build()
    for inp in ({}, {**_valid_input(), "gap_analysis": "   "}):
        result = await _call(forge, inp)
        assert result.is_error
        assert "gap_analysis" in str(result.content)


@pytest.mark.parametrize("bad_name", ["has space", "a" * 65, "", "dots.bad", 42])
async def test_forge_bad_name_is_error(bad_name: Any) -> None:
    forge, _, _, _ = _build()
    result = await _call(forge, {**_valid_input(), "name": bad_name})
    assert result.is_error
    assert "name" in str(result.content)


async def test_forge_name_collision_is_error() -> None:
    forge, _, _, registry = _build()
    clashing = build_forge_tool(CandidateStore(), registry)  # any tool named forge_tool
    registry.register(clashing)
    result = await _call(forge, {**_valid_input(), "name": "forge_tool"})
    assert result.is_error
    assert "already registered" in str(result.content)


async def test_forge_missing_description_is_error() -> None:
    forge, _, _, _ = _build()
    result = await _call(forge, {**_valid_input(), "description": ""})
    assert result.is_error
    assert "description" in str(result.content)


@pytest.mark.parametrize(
    "bad_schema",
    [
        "not a dict",
        {"type": "array"},
        {"type": "object"},
        {"type": "object", "properties": "nope"},
        {"type": "object", "properties": {"x": "not a schema"}},
        {"type": "object", "properties": {}, "required": ["ghost"]},
        {"type": "object", "properties": {}, "required": "x"},
    ],
)
async def test_forge_bad_input_schema_is_error(bad_schema: Any) -> None:
    forge, _, _, _ = _build()
    result = await _call(forge, {**_valid_input(), "input_schema": bad_schema})
    assert result.is_error
    assert "input_schema" in str(result.content)


async def test_forge_missing_behavior_is_error() -> None:
    forge, _, _, _ = _build()
    result = await _call(forge, {**_valid_input(), "behavior": "  "})
    assert result.is_error
    assert "behavior" in str(result.content)


@pytest.mark.parametrize(
    "bad_domains",
    ["api.example.com", ["https://x.com"], ["x.com/path"], [""], [42]],
)
async def test_forge_bad_allowed_domains_is_error(bad_domains: Any) -> None:
    forge, _, _, _ = _build()
    result = await _call(forge, {**_valid_input(), "allowed_domains": bad_domains})
    assert result.is_error
    assert "allowed_domains" in str(result.content)


@pytest.mark.parametrize(
    "bad_examples",
    [
        "not a list",
        [{"input": {}}],
        [{"output": "x"}],
        [{"input": "not a dict", "output": "x"}],
        [{"input": {}, "output": 42}],
    ],
)
async def test_forge_bad_examples_is_error(bad_examples: Any) -> None:
    forge, _, _, _ = _build()
    result = await _call(forge, {**_valid_input(), "examples": bad_examples})
    assert result.is_error
    assert "examples" in str(result.content)


async def test_forge_valid_optional_fields_accepted() -> None:
    forge, _, _, _ = _build()
    inp = {
        **_valid_input(),
        "allowed_domains": ["api.example.com"],
        "examples": [{"input": {"url": "https://x.com/feed"}, "output": "Title: hi"}],
    }
    result = await _call(forge, inp)
    # Optional fields are valid, so the only remaining error is the stub itself.
    assert "not yet implemented" in str(result.content)


# ── stub behavior ────────────────────────────────────────────────────────────


async def test_forge_valid_input_returns_guided_error() -> None:
    forge, _, store, _ = _build()
    result = await _call(forge, _valid_input())
    assert result.is_error
    content = str(result.content)
    assert content.startswith("[forge_tool error:")
    assert "not yet implemented" in content
    assert "Do NOT retry" in content
    # No candidate exists until a build actually succeeds.
    assert not store.has("fetch_rss")


async def test_register_missing_evidence_is_error() -> None:
    _, register, _, _ = _build()
    result = await _call(register, {"name": "fetch_rss"})
    assert result.is_error
    assert "holdout_evidence" in str(result.content)


async def test_register_missing_name_is_error() -> None:
    _, register, _, _ = _build()
    result = await _call(register, {"holdout_evidence": "ran 3 unseen feeds"})
    assert result.is_error
    assert "name" in str(result.content)


async def test_register_unknown_candidate_is_error() -> None:
    _, register, _, _ = _build()
    result = await _call(register, {"holdout_evidence": "ran 3 unseen feeds", "name": "fetch_rss"})
    assert result.is_error
    assert "no candidate named" in str(result.content)


async def test_register_seeded_candidate_returns_guided_error() -> None:
    _, register, store, registry = _build()
    store.put(_candidate())
    result = await _call(
        register, {"holdout_evidence": "ran 3 unseen feeds; outputs matched", "name": "fetch_rss"}
    )
    assert result.is_error
    content = str(result.content)
    assert content.startswith("[register_tool error:")
    assert "not yet implemented" in content
    assert "Do NOT retry" in content
    # Nothing was promoted and the candidate survives for the real registration.
    assert store.has("fetch_rss")
    assert not registry.has("fetch_rss")


# ── coexistence with the seed tool ───────────────────────────────────────────


def test_all_three_tools_coregister(sandbox_settings: SandboxSettings) -> None:
    registry = ToolRegistry(ToolContext())
    registry.register(build_run_bash(BashSandbox(sandbox_settings, runner=FakeRunner([]))))
    candidates = CandidateStore()
    registry.register(build_forge_tool(candidates, registry))
    registry.register(build_register_tool(candidates, registry))
    assert {s["name"] for s in registry.get_schemas()} == {
        "run_bash",
        "forge_tool",
        "register_tool",
    }


async def test_execute_through_registry_wraps_trusted() -> None:
    _, _, store, registry = _build()
    registry.register(build_forge_tool(store, registry))
    result = await registry.execute("forge_tool", _valid_input())
    assert isinstance(result.content, str)
    assert 'trust="TRUSTED"' in result.content
    assert "prompt_injection_warning" not in result.content
