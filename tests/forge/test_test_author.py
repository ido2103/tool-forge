"""Test-author unit tests — authoring loop, validation pipeline, feedback content.

Uses the scripted FakeProviderClient (no API) and FakeRunner (no Docker); the
real round trip lives in test_test_author_live.py behind the ``live`` marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.orchestrator._harness import FakeProviderClient, assistant_text
from tests.sandbox.test_bash import FakeRunner
from toolforge.config import SandboxSettings, TestAuthorSettings
from toolforge.forge.candidates import ToolSpec
from toolforge.forge.test_author import (
    TestAuthor,
    TestAuthorError,
    extract_python_block,
    parse_collect_count,
    parse_stub_run,
    screen_test_source,
)
from toolforge.providers import Message
from toolforge.sandbox import BashSandbox

# ── scripted materials ───────────────────────────────────────────────────────

GOOD_SOURCE = """\
from tool import run
import pytest


def test_simple():  # case 1
    assert run(text="Hello World") == "hello-world"


def test_empty():  # case 2
    assert run(text="") == ""


def test_punctuation():  # case 3
    assert run(text="a.b,c") == "a-b-c"


def test_unicode():  # case 4
    assert run(text="caf\\u00e9") == "cafe"


def test_missing_arg():  # case 5
    with pytest.raises(TypeError):
        run()
"""


def good_reply() -> Message:
    return assistant_text(
        "## Edge-case analysis\n1. basic\n2. empty\n3. punctuation\n4. unicode\n"
        f"5. missing argument\n\n```python\n{GOOD_SOURCE}```"
    )


INSTALL_OK = (0, b"")
COLLECT_OK = (0, b"5 tests collected in 0.01s")
COLLECT_TOO_FEW = (0, b"2 tests collected in 0.01s")
STUB_RED = (
    1,
    b"test_tool.py::test_simple FAILED\n"
    b"test_tool.py::test_empty FAILED\n"
    b"test_tool.py::test_punctuation FAILED\n"
    b"test_tool.py::test_unicode FAILED\n"
    b"test_tool.py::test_missing_arg FAILED\n"
    b"5 failed in 0.05s\n",
)
STUB_ONE_VACUOUS = (
    1,
    b"test_tool.py::test_simple PASSED\n"
    b"test_tool.py::test_empty FAILED\n"
    b"4 failed, 1 passed in 0.05s\n",
)


@pytest.fixture
def author_settings() -> TestAuthorSettings:
    return TestAuthorSettings(
        _env_file=None, model=None, max_attempts=3, max_tokens=2048, min_tests=5
    )


@pytest.fixture
def spec() -> ToolSpec:
    return ToolSpec(
        name="slugify",
        description="Turn text into a URL slug.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "text to slugify"}},
            "required": ["text"],
        },
        behavior="Lowercase, non-alphanumerics collapse to single hyphens.",
        examples=({"input": {"text": "Hello World"}, "output": "hello-world"},),
    )


def make_author(
    script: list[Message | Exception],
    results: list[tuple[int | None, bytes]],
    sandbox_settings: SandboxSettings,
    author_settings: TestAuthorSettings,
) -> tuple[TestAuthor, FakeProviderClient, FakeRunner]:
    client = FakeProviderClient(script)
    runner = FakeRunner([(0, b"started"), *results])
    sandbox = BashSandbox(sandbox_settings, runner=runner)
    author = TestAuthor(client, sandbox, sandbox_settings, author_settings, model="claude-test")
    return author, client, runner


def build_dir(sandbox_settings: SandboxSettings) -> Path:
    return sandbox_settings.workspace_path / "build" / "slugify"


# ── happy path ───────────────────────────────────────────────────────────────


async def test_happy_path(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, runner = make_author(
        [good_reply()], [INSTALL_OK, COLLECT_OK, STUB_RED], sandbox_settings, author_settings
    )
    result = await author.author_tests(spec)

    assert result.test_path == "/workspace/build/slugify/test_tool.py"
    assert result.test_count == 5
    assert result.attempts == 1
    assert "FAILED" in result.report

    # Host-side artifacts: the suite persists, the stub must not.
    assert (build_dir(sandbox_settings) / "test_tool.py").read_text() == GOOD_SOURCE
    assert not (build_dir(sandbox_settings) / "tool.py").exists()

    # The model call is attributed and routed correctly.
    assert client.calls[0]["component"] == "test_author"
    assert client.calls[0]["model"] == "claude-test"
    assert client.calls[0]["tools"] is None
    assert "Edge-case analysis" in client.calls[0]["system"]
    assert "slugify" in client.calls[0]["messages"][0].text

    # Sandbox commands: install, collect, stub run — in that order.
    install_cmd, collect_cmd, stub_cmd = (c[-1] for c in runner.calls[1:])
    assert "pip install" in install_cmd
    assert "--collect-only" in collect_cmd
    assert "cd /workspace/build/slugify" in collect_cmd
    assert "-v --tb=line" in stub_cmd


# ── per-failure-class retries ────────────────────────────────────────────────


async def test_no_python_block_retries_with_feedback(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, _ = make_author(
        [assistant_text("I would write tests but here is prose."), good_reply()],
        [INSTALL_OK, COLLECT_OK, STUB_RED],
        sandbox_settings,
        author_settings,
    )
    result = await author.author_tests(spec)
    assert result.attempts == 2
    feedback = client.calls[1]["messages"][-1].text
    assert "0 fenced" in feedback
    assert "exactly one is required" in feedback


async def test_two_python_blocks_rejected(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    two_blocks = assistant_text(
        f"```python\n{GOOD_SOURCE}```\nand also\n```python\nprint('extra')\n```"
    )
    author, client, _ = make_author(
        [two_blocks, good_reply()],
        [INSTALL_OK, COLLECT_OK, STUB_RED],
        sandbox_settings,
        author_settings,
    )
    result = await author.author_tests(spec)
    assert result.attempts == 2
    assert "2 fenced" in client.calls[1]["messages"][-1].text


async def test_forbidden_import_rejected_before_running(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    bad = assistant_text(f"```python\nimport socket\n{GOOD_SOURCE}```")
    author, client, runner = make_author(
        [bad, good_reply()], [INSTALL_OK, COLLECT_OK, STUB_RED], sandbox_settings, author_settings
    )
    result = await author.author_tests(spec)
    assert result.attempts == 2
    feedback = client.calls[1]["messages"][-1].text
    assert "socket" in feedback
    # The bad attempt never reached the sandbox: start + install + 2 runs only.
    assert len(runner.calls) == 4


async def test_collect_failure_feeds_output_back(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, _ = make_author(
        [good_reply(), good_reply()],
        [INSTALL_OK, (2, b"E   SyntaxError: invalid syntax"), COLLECT_OK, STUB_RED],
        sandbox_settings,
        author_settings,
    )
    result = await author.author_tests(spec)
    assert result.attempts == 2
    feedback = client.calls[1]["messages"][-1].text
    assert "could not collect" in feedback
    assert "SyntaxError" in feedback


async def test_too_few_tests_rejected(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, _ = make_author(
        [good_reply(), good_reply()],
        [INSTALL_OK, COLLECT_TOO_FEW, COLLECT_OK, STUB_RED],
        sandbox_settings,
        author_settings,
    )
    result = await author.author_tests(spec)
    assert result.attempts == 2
    feedback = client.calls[1]["messages"][-1].text
    assert "Only 2 tests collected" in feedback
    assert "at least 5" in feedback


async def test_vacuous_pass_names_the_test(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, _ = make_author(
        [good_reply(), good_reply()],
        [INSTALL_OK, COLLECT_OK, STUB_ONE_VACUOUS, COLLECT_OK, STUB_RED],
        sandbox_settings,
        author_settings,
    )
    result = await author.author_tests(spec)
    assert result.attempts == 2
    feedback = client.calls[1]["messages"][-1].text
    assert "test_tool.py::test_simple" in feedback
    assert "assert nothing" in feedback


async def test_stub_run_internal_error_feeds_back(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, _ = make_author(
        [good_reply(), good_reply()],
        [INSTALL_OK, COLLECT_OK, (2, b"INTERNALERROR> boom"), COLLECT_OK, STUB_RED],
        sandbox_settings,
        author_settings,
    )
    result = await author.author_tests(spec)
    assert result.attempts == 2
    feedback = client.calls[1]["messages"][-1].text
    assert "exit code 2" in feedback


# ── terminal failures ────────────────────────────────────────────────────────


async def test_budget_exhaustion_cleans_up(
    sandbox_settings: SandboxSettings, spec: ToolSpec
) -> None:
    settings = TestAuthorSettings(_env_file=None, max_attempts=2, min_tests=5)
    author, client, _ = make_author(
        [good_reply(), good_reply()],
        [INSTALL_OK, COLLECT_OK, STUB_ONE_VACUOUS, COLLECT_OK, STUB_ONE_VACUOUS],
        sandbox_settings,
        settings,
    )
    with pytest.raises(TestAuthorError, match="failed after 2 attempts"):
        await author.author_tests(spec)
    assert len(client.calls) == 2
    assert not build_dir(sandbox_settings).exists()


async def test_pytest_install_failure_is_immediate(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, _ = make_author(
        [], [(1, b"pip: no network")], sandbox_settings, author_settings
    )
    with pytest.raises(TestAuthorError, match="pytest could not be installed"):
        await author.author_tests(spec)
    assert client.calls == []


async def test_deadline_expiry_before_model_call(
    sandbox_settings: SandboxSettings, author_settings: TestAuthorSettings, spec: ToolSpec
) -> None:
    author, client, _ = make_author([], [INSTALL_OK], sandbox_settings, author_settings)
    clock_values = iter([0.0, float(author_settings.timeout_seconds + 1)])
    author._clock = lambda: next(clock_values)
    with pytest.raises(TestAuthorError, match="timed out .*before the model call"):
        await author.author_tests(spec)
    assert client.calls == []


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_extract_python_block_single() -> None:
    assert extract_python_block("prose\n```python\nx = 1\n```\nmore") == "x = 1\n"


def test_extract_python_block_none_or_many() -> None:
    assert extract_python_block("no code") is None
    assert extract_python_block("```python\na\n```\n```python\nb\n```") is None


def test_screen_test_source_clean() -> None:
    assert screen_test_source(GOOD_SOURCE) == []


def test_screen_test_source_problems() -> None:
    problems = screen_test_source("import subprocess\nimport urllib.request\nx = 1\n")
    text = "; ".join(problems)
    assert "subprocess" in text
    assert "urllib" in text
    assert "from tool import run" in text
    assert "def test_" in text


def test_parse_collect_count() -> None:
    assert parse_collect_count("5 tests collected in 0.01s") == 5
    assert parse_collect_count("1 test collected in 0.01s") == 1
    assert parse_collect_count("garbage") is None


def test_parse_stub_run() -> None:
    out = "test_tool.py::test_a PASSED\ntest_tool.py::test_b FAILED\ntest_tool.py::test_c ERROR\n"
    outcome = parse_stub_run(out)
    assert outcome.passed == ["test_tool.py::test_a"]
    assert outcome.failed == ["test_tool.py::test_b"]
    assert outcome.errors == ["test_tool.py::test_c"]
