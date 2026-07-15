# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Environment is managed with `uv` (Python 3.12+); run everything through `uv run`.

```bash
uv sync                                        # install/refresh deps (incl. dev group)
uv run pytest                                  # run all tests
uv run pytest tests/test_smoke.py::test_package_imports   # run a single test
uv run ruff check .                            # lint
uv run ruff format .                           # format
uv run ruff format --check .                   # format check (what CI runs)
uv run mypy                                    # type-check (strict; targets src/ and tests/)
```

## Git workflow

- **Never commit on `main`.** Always check out a feature branch first
  (`git checkout -b <type>/<short-name>`, e.g. `feat/wall-detector`).
- **Committing runs checks automatically** via pre-commit hooks
  (`.pre-commit-config.yaml`): ruff check (with `--fix`), ruff format, and mypy. If a
  hook fails or rewrites files, re-stage and commit again. `pytest` is NOT hooked —
  run `uv run pytest` manually and pass before every commit.
- **PRs**: when a branch is ready, proactively suggest opening a PR, but always ask
  the user for permission before actually creating one.

## Contracts

These are standing rules for every session working in this repo:

1. **Documentation contract.** Any change to code under `src/toolforge/<area>/` MUST
   update `docs/<area>.md` in the same commit (at minimum its `Status:` line and any
   behavior it describes). Changes that alter how subsystems connect also update
   `docs/architecture.md`. A new subsystem gets a new `docs/<name>.md`, a line in
   `docs/architecture.md`, and a mention in this file. `docs/spec.md` is the verbatim
   original design handoff — never edit it; diverging decisions are recorded in the
   architecture/subsystem docs instead.
2. **Granularity principle.** When designing tools (and modules), prefer composable
   primitives (`browser_click`, `browser_read`) over task-specific mega-tools
   (`check_my_email`).

## What this project is

An agent system that, when it lacks a tool for a task, **forges the tool itself**:
spec → adversarial tests → implementation → sandbox verification → registration. The
toolbox grows over time; a skills library captures multi-step workflows. Portfolio
project — the eval graphs are a first-class deliverable.

Full design: [docs/spec.md](docs/spec.md) (verbatim handoff, source of truth) and
[docs/architecture.md](docs/architecture.md) (living overview).

## Architecture

**Model split** — frontier tokens for decisions, cheap tokens for sweat:
- *Orchestrator* (frontier API model, Claude Sonnet/Opus): task execution, wall
  detection, tool-spec and adversarial-test authoring, skill authoring, satisfaction
  review. All judgment lives here.
- *Forge worker* (configurable backend): implements tools against failing tests until
  green. Two first-class modes — **api** (default; a cheaper API model, e.g. Claude
  Haiku; no local hardware required) and **local** (Qwen3.6-35B-A3B or Qwen3.6-27B via
  any OpenAI-compatible endpoint). Invariant in both: the worker is never the same
  model as the orchestrator — cross-model separation mitigates the self-verification
  trap.

**Core loop**: orchestrator works a task with registered tools → on failure the wall
detector classifies (missing tool / misuse / impossible) → missing tool: check registry
for reuse/composition first → else forge (frontier writes adversarial tests from spec
only; the forge worker implements with a docs-RAG tool under a bounded iteration
budget) →
orchestrator holdout check (**green tests alone never register a tool**) → tool + spec +
tests + companion usage skill registered; the harness appends the new tool schema to
later API calls (the model never edits its own payload). v1 forges mid-task
(pause/build/resume).

**Subsystems** (`src/toolforge/<name>/`, each documented in `docs/<name>.md`):
orchestrator, forge, registry, skills, sandbox, evals.

**Safety**: all generated code runs in the sandbox — container, no network by default,
per-tool allowlisted domains; every credential access and execution is logged;
dev/testing uses throwaway accounts only.

**Evals** (produce the README graphs): tool reuse rate over time, composition depth
(tools built from tools), held-out task success rate over time.
