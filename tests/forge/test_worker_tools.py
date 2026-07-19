"""Worker-tool unit tests — write_tool_code screening and run_tests plumbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.sandbox.test_bash import FakeRunner
from toolforge.config import SandboxSettings
from toolforge.forge.worker_tools import build_run_tests, build_write_tool_code
from toolforge.registry import ToolContext
from toolforge.sandbox import BashSandbox
from toolforge.sandbox.run_bash import SANDBOX_SERIAL_GROUP

CTX = ToolContext()


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    return tmp_path / "workspace" / "build" / "slugify"


# ── write_tool_code ──────────────────────────────────────────────────────────


async def test_write_writes_and_replaces(build_dir: Path) -> None:
    tool = build_write_tool_code(build_dir, "slugify")
    first = await tool.handler({"code": "def run(text):\n    return text\n"}, CTX)
    assert not first.is_error
    assert "wrote /workspace/build/slugify/tool.py" in first.content
    assert "run_tests" in first.content

    second = await tool.handler({"code": "def run(text):\n    return text.lower()\n"}, CTX)
    assert not second.is_error
    assert (build_dir / "tool.py").read_text() == "def run(text):\n    return text.lower()\n"


async def test_write_rejects_blank_code(build_dir: Path) -> None:
    tool = build_write_tool_code(build_dir, "slugify")
    result = await tool.handler({"code": "   "}, CTX)
    assert result.is_error
    assert not (build_dir / "tool.py").exists()


async def test_write_rejects_syntax_error_with_line(build_dir: Path) -> None:
    tool = build_write_tool_code(build_dir, "slugify")
    result = await tool.handler({"code": "def run(:\n    pass\n"}, CTX)
    assert result.is_error
    assert "syntax error at line 1" in result.content
    assert not (build_dir / "tool.py").exists()


async def test_write_rejects_third_party_import(build_dir: Path) -> None:
    tool = build_write_tool_code(build_dir, "slugify")
    result = await tool.handler(
        {"code": "import requests\n\ndef run(url):\n    return requests.get(url).text\n"}, CTX
    )
    assert result.is_error
    assert "'requests'" in result.content
    assert "standard library" in result.content
    assert not (build_dir / "tool.py").exists()


async def test_write_rejects_nested_and_from_imports(build_dir: Path) -> None:
    tool = build_write_tool_code(build_dir, "slugify")
    code = "def run(x):\n    from pandas import DataFrame\n    return DataFrame([x])\n"
    result = await tool.handler({"code": code}, CTX)
    assert result.is_error
    assert "'pandas'" in result.content


async def test_write_accepts_stdlib_imports(build_dir: Path) -> None:
    tool = build_write_tool_code(build_dir, "slugify")
    code = (
        "import json\nimport urllib.request\nfrom collections import Counter\n\n"
        "def run(text):\n    return json.dumps(dict(Counter(text)))\n"
    )
    result = await tool.handler({"code": code}, CTX)
    assert not result.is_error
    assert (build_dir / "tool.py").read_text() == code


def test_write_serial_group_and_schema(build_dir: Path) -> None:
    tool = build_write_tool_code(build_dir, "slugify")
    assert tool.serial_group == SANDBOX_SERIAL_GROUP
    assert tool.input_schema["required"] == ["code"]
    assert "/workspace/build/slugify/tool.py" in tool.description


# ── run_tests ────────────────────────────────────────────────────────────────


def _sandbox(
    sandbox_settings: SandboxSettings, results: list[tuple[int | None, bytes]]
) -> tuple[BashSandbox, FakeRunner]:
    runner = FakeRunner([(0, b"started"), *results])
    return BashSandbox(sandbox_settings, runner=runner), runner


async def test_run_tests_green(sandbox_settings: SandboxSettings) -> None:
    sandbox, runner = _sandbox(sandbox_settings, [(0, b"5 passed in 0.05s")])
    tool = build_run_tests(sandbox, "slugify")
    result = await tool.handler({}, CTX)
    assert not result.is_error
    assert "5 passed" in result.content
    command = runner.calls[-1][-1]
    assert "cd /workspace/build/slugify" in command
    assert "-p no:cacheprovider" in command
    assert "test_tool.py" in command


async def test_run_tests_red_is_error(sandbox_settings: SandboxSettings) -> None:
    sandbox, _ = _sandbox(sandbox_settings, [(1, b"test_tool.py::test_x FAILED")])
    tool = build_run_tests(sandbox, "slugify")
    result = await tool.handler({}, CTX)
    assert result.is_error
    assert "FAILED" in result.content


async def test_run_tests_timeout_is_error(sandbox_settings: SandboxSettings) -> None:
    sandbox, _ = _sandbox(sandbox_settings, [(None, b"partial output")])
    tool = build_run_tests(sandbox, "slugify")
    result = await tool.handler({}, CTX)
    assert result.is_error


def test_run_tests_serial_group_and_trust(sandbox_settings: SandboxSettings) -> None:
    sandbox, _ = _sandbox(sandbox_settings, [])
    tool = build_run_tests(sandbox, "slugify")
    assert tool.serial_group == SANDBOX_SERIAL_GROUP
    # network="none" in the fixture → output cannot carry fetched text.
    assert tool.trust == "TRUSTED"
    assert "advisory" in tool.description
