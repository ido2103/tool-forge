"""Live forge-worker round trip — real models + real Docker sandbox.

    uv run pytest -m live tests/forge/test_worker_live.py

A small offline spec goes through the full build pipeline: the test author
(orchestrator-tier model) writes the red suite, then the forge worker (backend
from env; default api / claude-haiku-4-5) implements against it inside the
container until the harness verification is green. Requires Anthropic
credentials and the docker daemon; needs sandbox network to pip-install
pytest. Skips (not fails) when either piece is missing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests._docker import DOCKER_SKIP_REASON, docker_available
from toolforge.config import (
    AnthropicSettings,
    SandboxSettings,
    TestAuthorSettings,
    WorkerSettings,
)
from toolforge.forge import ForgeWorker, TestAuthor, ToolSpec
from toolforge.providers import AnthropicClient, OpenAICompatClient, ProviderClient
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


async def test_author_then_worker_builds_green(tmp_path_factory: pytest.TempPathFactory) -> None:
    anthropic = _anthropic_settings_or_skip()
    worker_settings = WorkerSettings()
    author_settings = TestAuthorSettings()
    tmp = tmp_path_factory.mktemp("live-worker")
    sandbox_settings = SandboxSettings(
        _env_file=None,
        image="python:3.12-slim",
        network="on",  # pip install pytest at forge time
        workspace_path=tmp / "workspace",
        tools_path=tmp / "tools",
        command_timeout=120,
        output_cap=100_000,
    )

    client = AnthropicClient(anthropic)
    worker_client: ProviderClient = (
        client if worker_settings.backend == "api" else OpenAICompatClient(worker_settings)
    )
    sandbox = BashSandbox(sandbox_settings)
    try:
        author = TestAuthor(
            client,
            sandbox,
            sandbox_settings,
            author_settings,
            model=author_settings.model or anthropic.model,
        )
        worker = ForgeWorker(
            worker_client,
            sandbox,
            sandbox_settings,
            worker_settings,
            model=worker_settings.effective_model,
            runs_dir=tmp / "runs",
        )

        tests = await author.author_tests(_SPEC)
        assert tests.test_count >= author_settings.min_tests

        built = await worker.build(_SPEC, tests)
        assert built.attempts >= 1
        assert "def run" in built.code
        assert built.code_path == "/workspace/build/slugify/tool.py"
        # The pristine suite passed in full under the harness's own run.
        assert f"{tests.test_count} passed" in built.test_report
        # The worker conversation was persisted.
        assert list((tmp / "runs").glob("forge-slugify-*.jsonl"))
    finally:
        sandbox.teardown()
