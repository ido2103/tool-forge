"""forge_tool / register_tool tests — schemas, validation, pipeline, promotion."""

from __future__ import annotations

from typing import Any

import pytest

from toolforge.config import SandboxSettings
from toolforge.forge import (
    AuthoredTests,
    BuildResult,
    Candidate,
    CandidateStore,
    ForgeWorker,
    TestAuthor,
    TestAuthorError,
    ToolSpec,
    WorkerError,
    build_forge_tool,
    build_register_tool,
)
from toolforge.forge.manifest import load_manifest
from toolforge.registry import RegisteredTool, ToolContext, ToolRegistry, ToolResult
from toolforge.sandbox.bash import BashSandbox
from toolforge.sandbox.run_bash import SANDBOX_SERIAL_GROUP, build_run_bash

from tests.sandbox.test_bash import FakeRunner

AUTHORED = AuthoredTests(
    test_path="/workspace/build/fetch_rss/test_tool.py",
    test_count=5,
    report="5 failed (stub run)",
    attempts=1,
)

BUILT = BuildResult(
    code="def run(url):\n    return url\n",
    code_path="/workspace/build/fetch_rss/tool.py",
    test_path=AUTHORED.test_path,
    test_report="test_tool.py::test_a PASSED\n5 passed in 0.03s",
    attempts=2,
)


class FakeTestAuthor(TestAuthor):
    """Scripted stand-in: returns the outcome, or raises it. Records specs."""

    def __init__(self, outcome: AuthoredTests | TestAuthorError = AUTHORED) -> None:
        self._outcome = outcome
        self.specs: list[ToolSpec] = []

    async def author_tests(self, spec: ToolSpec) -> AuthoredTests:
        self.specs.append(spec)
        if isinstance(self._outcome, TestAuthorError):
            raise self._outcome
        return self._outcome


class FakeWorker(ForgeWorker):
    """Scripted stand-in: returns the outcome, or raises it. Records builds."""

    def __init__(self, outcome: BuildResult | WorkerError = BUILT) -> None:
        self._outcome = outcome
        self.builds: list[tuple[ToolSpec, AuthoredTests]] = []

    async def build(self, spec: ToolSpec, tests: AuthoredTests) -> BuildResult:
        self.builds.append((spec, tests))
        if isinstance(self._outcome, WorkerError):
            raise self._outcome
        return self._outcome


class ForgeEnv:
    """The REPL's forge object graph, built on a fake sandbox runner."""

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        author_outcome: AuthoredTests | TestAuthorError = AUTHORED,
        worker_outcome: BuildResult | WorkerError = BUILT,
    ) -> None:
        self.settings = settings
        self.store = CandidateStore()
        self.registry = ToolRegistry(ToolContext())
        self.runner = FakeRunner([])
        self.sandbox = BashSandbox(settings, runner=self.runner)
        self.author = FakeTestAuthor(author_outcome)
        self.worker = FakeWorker(worker_outcome)
        self.forge = build_forge_tool(
            self.store, self.registry, test_author=self.author, worker=self.worker
        )
        self.register = build_register_tool(self.store, self.registry, self.sandbox, settings)


@pytest.fixture
def env(sandbox_settings: SandboxSettings) -> ForgeEnv:
    return ForgeEnv(sandbox_settings)


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


def _candidate(env: ForgeEnv, name: str = "fetch_rss", *, with_files: bool = True) -> Candidate:
    """A candidate as the (future) build loop would leave it: files in the workspace."""
    spec = _valid_input()
    candidate = Candidate(
        name=name,
        description=spec["description"],
        input_schema=spec["input_schema"],
        behavior=spec["behavior"],
        gap_analysis=spec["gap_analysis"],
    )
    if with_files:
        build_dir = env.settings.workspace_path / "build" / name
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "tool.py").write_text("def run(url):\n    return url\n")
        candidate.code_path = f"/workspace/build/{name}/tool.py"
    return candidate


async def _call(tool: RegisteredTool, inp: dict[str, Any]) -> ToolResult:
    return await tool.handler(inp, ToolContext())


def _register_input(name: str = "fetch_rss") -> dict[str, Any]:
    return {"holdout_evidence": "ran 3 unseen feeds; outputs matched", "name": name}


# ── schema shape ─────────────────────────────────────────────────────────────


def test_forge_schema_shape(env: ForgeEnv) -> None:
    assert env.forge.name == "forge_tool"
    assert env.forge.trust == "TRUSTED"
    # gap_analysis first: generation order is reasoning order — the model must
    # commit to the reuse check before it starts designing the new tool.
    assert env.forge.input_schema["required"] == [
        "gap_analysis",
        "name",
        "description",
        "input_schema",
        "behavior",
    ]
    assert set(env.forge.input_schema["properties"]) == {
        "gap_analysis",
        "name",
        "description",
        "input_schema",
        "behavior",
        "allowed_domains",
        "examples",
    }


def test_register_schema_shape(env: ForgeEnv) -> None:
    assert env.register.name == "register_tool"
    assert env.register.trust == "TRUSTED"
    assert env.register.input_schema["required"] == ["holdout_evidence", "name"]


# ── forge_tool validation ────────────────────────────────────────────────────


async def test_forge_missing_gap_analysis_is_error(env: ForgeEnv) -> None:
    for inp in ({}, {**_valid_input(), "gap_analysis": "   "}):
        result = await _call(env.forge, inp)
        assert result.is_error
        assert "gap_analysis" in str(result.content)


@pytest.mark.parametrize("bad_name", ["has space", "a" * 65, "", "dots.bad", 42])
async def test_forge_bad_name_is_error(env: ForgeEnv, bad_name: Any) -> None:
    result = await _call(env.forge, {**_valid_input(), "name": bad_name})
    assert result.is_error
    assert "name" in str(result.content)


async def test_forge_name_collision_is_error(env: ForgeEnv) -> None:
    # Any registered tool named forge_tool works as the clashing entry.
    clashing = build_forge_tool(
        CandidateStore(), env.registry, test_author=FakeTestAuthor(), worker=FakeWorker()
    )
    env.registry.register(clashing)
    result = await _call(env.forge, {**_valid_input(), "name": "forge_tool"})
    assert result.is_error
    assert "already registered" in str(result.content)


async def test_forge_missing_description_is_error(env: ForgeEnv) -> None:
    result = await _call(env.forge, {**_valid_input(), "description": ""})
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
async def test_forge_bad_input_schema_is_error(env: ForgeEnv, bad_schema: Any) -> None:
    result = await _call(env.forge, {**_valid_input(), "input_schema": bad_schema})
    assert result.is_error
    assert "input_schema" in str(result.content)


async def test_forge_missing_behavior_is_error(env: ForgeEnv) -> None:
    result = await _call(env.forge, {**_valid_input(), "behavior": "  "})
    assert result.is_error
    assert "behavior" in str(result.content)


@pytest.mark.parametrize(
    "bad_domains",
    ["api.example.com", ["https://x.com"], ["x.com/path"], [""], [42]],
)
async def test_forge_bad_allowed_domains_is_error(env: ForgeEnv, bad_domains: Any) -> None:
    result = await _call(env.forge, {**_valid_input(), "allowed_domains": bad_domains})
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
async def test_forge_bad_examples_is_error(env: ForgeEnv, bad_examples: Any) -> None:
    result = await _call(env.forge, {**_valid_input(), "examples": bad_examples})
    assert result.is_error
    assert "examples" in str(result.content)


async def test_forge_valid_optional_fields_accepted(env: ForgeEnv) -> None:
    inp = {
        **_valid_input(),
        "allowed_domains": ["api.example.com"],
        "examples": [{"input": {"url": "https://x.com/feed"}, "output": "Title: hi"}],
    }
    result = await _call(env.forge, inp)
    assert not result.is_error
    # The optional fields flow into the spec handed to the build stages.
    spec = env.author.specs[0]
    assert spec.allowed_domains == ("api.example.com",)
    assert spec.examples


# ── forge_tool pipeline ──────────────────────────────────────────────────────


async def test_forge_success_stores_candidate_and_reports(env: ForgeEnv) -> None:
    result = await _call(env.forge, _valid_input())
    assert not result.is_error

    # The stages ran in order, on the spec derived from the input.
    assert env.author.specs[0].name == "fetch_rss"
    assert env.worker.builds[0][1] is AUTHORED

    # A fully-populated candidate awaits the holdout check — nothing registered.
    candidate = env.store.get("fetch_rss")
    assert candidate is not None
    assert candidate.code_path == BUILT.code_path
    assert candidate.test_path == BUILT.test_path
    assert candidate.test_report == BUILT.test_report
    assert candidate.gap_analysis
    assert not env.registry.has("fetch_rss")

    content = str(result.content)
    assert "candidate 'fetch_rss' built" in content
    assert "5 tests green" in content
    assert "2 worker attempt(s)" in content
    assert BUILT.code_path in content
    assert "def run(url):" in content
    assert "NOT registered" in content


async def test_forge_serial_group(env: ForgeEnv) -> None:
    # A build occupies the shared container for minutes; batched sibling
    # sandbox calls must queue behind it.
    assert env.forge.serial_group == SANDBOX_SERIAL_GROUP


async def test_forge_author_failure_maps_to_guided_error(
    sandbox_settings: SandboxSettings,
) -> None:
    env = ForgeEnv(
        sandbox_settings,
        author_outcome=TestAuthorError("authoring failed after 3 attempts; last failure: X"),
    )
    result = await _call(env.forge, _valid_input())
    assert result.is_error
    content = str(result.content)
    assert content.startswith("[forge_tool error: test authoring failed")
    assert "authoring failed after 3 attempts" in content
    assert "Revise the spec" in content
    assert not env.store.has("fetch_rss")
    assert not env.worker.builds  # the worker never ran


async def test_forge_worker_exhaustion_maps_to_guided_error(
    sandbox_settings: SandboxSettings,
) -> None:
    env = ForgeEnv(
        sandbox_settings,
        worker_outcome=WorkerError("tests still failing after 4 verification attempts"),
    )
    result = await _call(env.forge, _valid_input())
    assert result.is_error
    content = str(result.content)
    assert "could not make the test suite pass" in content
    assert "tests still failing after 4 verification attempts" in content
    assert "/workspace/build/fetch_rss/" in content
    assert "NOT a candidate" in content
    assert not env.store.has("fetch_rss")


async def test_forge_reforge_replaces_candidate(env: ForgeEnv) -> None:
    await _call(env.forge, _valid_input())
    stale = env.store.get("fetch_rss")
    await _call(env.forge, _valid_input())
    assert env.store.get("fetch_rss") is not stale  # revision path: put() replaces


# ── register_tool validation ─────────────────────────────────────────────────


async def test_register_missing_evidence_is_error(env: ForgeEnv) -> None:
    result = await _call(env.register, {"name": "fetch_rss"})
    assert result.is_error
    assert "holdout_evidence" in str(result.content)


async def test_register_missing_name_is_error(env: ForgeEnv) -> None:
    result = await _call(env.register, {"holdout_evidence": "ran 3 unseen feeds"})
    assert result.is_error
    assert "name" in str(result.content)


async def test_register_unknown_candidate_is_error(env: ForgeEnv) -> None:
    result = await _call(env.register, _register_input())
    assert result.is_error
    assert "no candidate named" in str(result.content)


# ── register_tool promotion ──────────────────────────────────────────────────


async def test_register_promotes_candidate(env: ForgeEnv) -> None:
    env.store.put(_candidate(env))
    result = await _call(env.register, _register_input())
    assert not result.is_error
    assert "'fetch_rss' registered" in str(result.content)
    # Candidate consumed; tool live with the forged-tool trust/serial contract.
    assert not env.store.has("fetch_rss")
    assert env.registry.has("fetch_rss")
    assert env.registry.trust_for("fetch_rss") == "UNVERIFIED"
    assert env.registry.serial_group_for("fetch_rss") == "sandbox"
    # Live-growth contract: the new schema is in the very next get_schemas().
    assert "fetch_rss" in {s["name"] for s in env.registry.get_schemas()}
    # Persisted: artifacts + provenance on disk.
    tool_dir = env.settings.tools_path / "fetch_rss"
    assert (tool_dir / "tool.py").is_file()
    manifest = load_manifest(tool_dir)
    assert manifest.holdout_evidence == "ran 3 unseen feeds; outputs matched"


async def test_register_missing_artifact_keeps_candidate(env: ForgeEnv) -> None:
    candidate = _candidate(env)
    env.store.put(candidate)
    (env.settings.workspace_path / "build" / "fetch_rss" / "tool.py").unlink()
    result = await _call(env.register, _register_input())
    assert result.is_error
    assert "missing from the workspace" in str(result.content)
    assert env.store.has("fetch_rss")  # kept for a re-forge + retry
    assert not env.registry.has("fetch_rss")
    assert not (env.settings.tools_path / "fetch_rss").exists()


async def test_register_codeless_candidate_is_error(env: ForgeEnv) -> None:
    env.store.put(_candidate(env, with_files=False))
    result = await _call(env.register, _register_input())
    assert result.is_error
    assert "no code artifact" in str(result.content)
    assert env.store.has("fetch_rss")


async def test_register_name_collision_is_error(env: ForgeEnv) -> None:
    env.store.put(_candidate(env))
    await _call(env.register, _register_input())
    # A second candidate under the now-registered name cannot be promoted.
    env.store.put(_candidate(env))
    result = await _call(env.register, _register_input())
    assert result.is_error
    assert "already registered" in str(result.content)
    assert env.store.has("fetch_rss")


async def test_register_existing_store_dir_is_error(env: ForgeEnv) -> None:
    env.store.put(_candidate(env))
    (env.settings.tools_path / "fetch_rss").mkdir(parents=True)
    result = await _call(env.register, _register_input())
    assert result.is_error
    assert "already exists" in str(result.content)
    assert env.store.has("fetch_rss")
    assert not env.registry.has("fetch_rss")


async def test_registered_tool_executes_via_sandbox(env: ForgeEnv) -> None:
    env.store.put(_candidate(env))
    await _call(env.register, _register_input())
    # Queue scripted results on the shared runner: docker run (start) + exec.
    env.runner._results.extend([(0, b"started"), (0, b"Title: hi\n")])
    result = await env.registry.execute("fetch_rss", {"url": "https://x.com/feed"})
    assert isinstance(result.content, str)
    assert "Title: hi" in result.content
    assert "prompt_injection_warning" in result.content  # UNVERIFIED envelope


# ── end-to-end through the agent loop ────────────────────────────────────────


async def test_register_makes_tool_callable_next_iteration(env: ForgeEnv) -> None:
    """The full mid-task growth story: register_tool in iteration 1, the forged
    tool called in iteration 2 — the harness appends the schema, the model never
    edits its own payload."""
    from tests.orchestrator._harness import FakeProviderClient, assistant_text, assistant_tool_use

    from toolforge.orchestrator.hooks import HookManager
    from toolforge.orchestrator.loop import Orchestrator

    env.store.put(_candidate(env))
    env.registry.register(env.register)
    # Scripted sandbox activity for the forged call: docker run (start) + exec.
    env.runner._results.extend([(0, b"started"), (0, b"Title: hi\n")])

    client = FakeProviderClient(
        [
            assistant_tool_use(("toolu_1", "register_tool", _register_input())),
            assistant_tool_use(("toolu_2", "fetch_rss", {"url": "https://x.com/feed"})),
            assistant_text("done"),
        ]
    )
    orch = Orchestrator(
        client=client,
        registry=env.registry,
        hooks=HookManager(),
        model="claude-test",
        max_tokens=1024,
        max_iterations=5,
    )
    result = await orch.run("forge me an rss tool", [], system_prompt="sys")

    assert result == "done"
    assert "fetch_rss" not in {t["name"] for t in client.calls[0]["tools"]}
    assert "fetch_rss" in {t["name"] for t in client.calls[1]["tools"]}
    # Iteration 3's history carries the forged tool's enveloped UNVERIFIED output.
    third_messages = client.calls[2]["messages"]
    flattened = str([getattr(b, "content", "") for m in third_messages for b in m.content])
    assert "Title: hi" in flattened
    assert "prompt_injection_warning" in flattened


# ── coexistence with the seed tool ───────────────────────────────────────────


def test_all_three_tools_coregister(sandbox_settings: SandboxSettings) -> None:
    registry = ToolRegistry(ToolContext())
    sandbox = BashSandbox(sandbox_settings, runner=FakeRunner([]))
    registry.register(build_run_bash(sandbox))
    candidates = CandidateStore()
    registry.register(
        build_forge_tool(candidates, registry, test_author=FakeTestAuthor(), worker=FakeWorker())
    )
    registry.register(build_register_tool(candidates, registry, sandbox, sandbox_settings))
    assert {s["name"] for s in registry.get_schemas()} == {
        "run_bash",
        "forge_tool",
        "register_tool",
    }


async def test_execute_through_registry_wraps_trusted(env: ForgeEnv) -> None:
    env.registry.register(env.forge)
    result = await env.registry.execute("forge_tool", _valid_input())
    assert isinstance(result.content, str)
    assert 'trust="TRUSTED"' in result.content
    assert "prompt_injection_warning" not in result.content
