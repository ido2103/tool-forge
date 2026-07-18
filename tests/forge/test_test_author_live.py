"""Live test-authoring round trip — real Anthropic model + real Docker sandbox.

    uv run pytest -m live tests/forge/test_test_author_live.py

A small spec goes through the full authoring pipeline: frontier model call →
single fenced block → static screen → in-container collect → all-red stub run.
Requires Anthropic credentials and the docker daemon; needs sandbox network to
pip-install pytest. Skips (not fails) when either piece is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tests._docker import DOCKER_SKIP_REASON, docker_available
from toolforge.config import AnthropicSettings, SandboxSettings, TestAuthorSettings
from toolforge.forge import TestAuthor, ToolSpec
from toolforge.providers import AnthropicClient
from toolforge.sandbox.bash import BashSandbox

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not docker_available(), reason=DOCKER_SKIP_REASON),
]


def _anthropic_settings_or_skip() -> AnthropicSettings:
    try:
        return AnthropicSettings()
    except ValidationError:
        pytest.skip("no Anthropic credentials configured (env vars / .env)")


_SPEC = ToolSpec(
    name="slugify",
    description="Turn arbitrary text into a URL-safe slug.",
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to slugify."},
        },
        "required": ["text"],
    },
    behavior=(
        "Lowercase the input. Every maximal run of characters that are not "
        "ASCII letters or digits becomes a single hyphen; non-ASCII characters "
        "are treated like any other non-alphanumeric character. Strip leading "
        "and trailing hyphens from the result. An empty string returns an "
        "empty string. Raises TypeError if text is not a string."
    ),
    examples=(
        {"input": {"text": "Hello, World!"}, "output": "hello-world"},
        {"input": {"text": "  --spaced out--  "}, "output": "spaced-out"},
    ),
)


async def test_author_round_trip(tmp_path: Path) -> None:
    anthropic = _anthropic_settings_or_skip()
    author_settings = TestAuthorSettings()
    sandbox_settings = SandboxSettings(
        _env_file=None,
        image="python:3.12-slim",
        network="on",  # pip install pytest needs the bridge network
        workspace_path=tmp_path / "workspace",
        tools_path=tmp_path / "tools",
        command_timeout=60,
        output_cap=100_000,
    )
    sandbox = BashSandbox(sandbox_settings)
    try:
        author = TestAuthor(
            AnthropicClient(anthropic),
            sandbox,
            sandbox_settings,
            author_settings,
            model=author_settings.model or anthropic.model,
        )
        result = await author.author_tests(_SPEC)

        assert result.test_count >= author_settings.min_tests
        assert result.attempts >= 1
        assert "FAILED" in result.report

        # The suite exists host-side; the stub was removed on success.
        build_dir = sandbox_settings.workspace_path / "build" / "slugify"
        suite = build_dir / "test_tool.py"
        assert suite.exists()
        assert "from tool import run" in suite.read_text()
        assert not (build_dir / "tool.py").exists()

        # Independent re-check: against a fresh stub, the suite is still red.
        (build_dir / "tool.py").write_text(
            'def run(**kwargs):\n    raise NotImplementedError("stub")\n'
        )
        rerun = await sandbox.run(
            "cd /workspace/build/slugify && python3 -m pytest -q -p no:cacheprovider test_tool.py",
            timeout=120,
        )
        assert rerun.exit_code == 1  # every test fails, no internal errors
        assert " passed" not in rerun.stdout
    finally:
        sandbox.teardown()
