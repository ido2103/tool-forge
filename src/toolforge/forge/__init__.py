"""Forge: turns a tool spec into a verified, registered tool.

Orchestrator-facing surface: two composable tools. ``forge_tool`` (stubbed)
takes a spec (gap analysis, name, description, input schema, behavior
contract) and builds a *candidate* — code + tests in the sandbox — without
registering anything. ``register_tool`` (implemented) promotes a candidate
into the live registry AND the on-disk tool store (``promote.py``; read-only
in the container, reloaded at boot by ``load_persisted_tools``), but only
after the orchestrator has independently verified it (holdout inputs via
run_bash and/or code review): green tests alone never register a tool.
Promoted tools execute through the harness-owned runner (``runner.py``,
installed by ``runtime.install_runner``) inside the sandbox container.

Internal loop: the **test author** (``test_author.py``, implemented) has a
frontier-tier model write adversarial tests from the spec alone (TDD, before
any implementation) and only accepts a suite that is collected and all-red
against a stub; then the **forge worker** (``worker.py``, implemented; api or
local backend, never the orchestrator's model) implements against them as an
agentic mini-loop under a config-bounded budget, verified green only by the
harness's pristine-suite run, escalating failures back to the orchestrator.
"""

from toolforge.forge.candidates import Candidate, CandidateStore, ToolSpec
from toolforge.forge.manifest import Manifest, ManifestError, load_manifest, write_manifest
from toolforge.forge.promote import PromotionError, load_persisted_tools, promote_candidate
from toolforge.forge.runtime import build_forged_tool, install_runner
from toolforge.forge.test_author import AuthoredTests, TestAuthor, TestAuthorError
from toolforge.forge.tools import build_forge_tool, build_register_tool
from toolforge.forge.worker import BuildResult, ForgeWorker, WorkerError

__all__ = [
    "AuthoredTests",
    "BuildResult",
    "Candidate",
    "CandidateStore",
    "ForgeWorker",
    "TestAuthor",
    "TestAuthorError",
    "ToolSpec",
    "WorkerError",
    "Manifest",
    "ManifestError",
    "PromotionError",
    "build_forge_tool",
    "build_forged_tool",
    "build_register_tool",
    "install_runner",
    "load_manifest",
    "load_persisted_tools",
    "promote_candidate",
    "write_manifest",
]
