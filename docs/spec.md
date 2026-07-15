# Project Handoff: Self-Expanding Tool-Forge Agent

> Original handoff spec, preserved verbatim. This is the source of truth for design
> decisions; docs/architecture.md and the per-subsystem docs derive from it.

## One-liner

An agent system that, when it lacks a tool for a task, forges the tool itself: spec → adversarial tests → implementation → sandbox verification → registration. The agent's toolbox grows over time; a skill library captures multi-step workflows. Portfolio project — the evals/graphs are a first-class deliverable.

## Model split (decided, with rationale)

- **Orchestrator: frontier API model (Claude Sonnet or Opus).**
  Owns all judgment: task execution, wall detection, tool spec authoring, adversarial test writing, skill authoring, final "satisfaction" review. Chosen because orchestration accumulates long context (tool registry, task history) and judgment calls degrade most in smaller models.
- **Forge worker: Qwen3.6-35B-A3B (local, Apache 2.0).**
  Owns labor: implements tools against failing tests, iterates until green. Strong agentic-coding benchmarks; knowledge gaps are patched externally (see RAG).
- Rationale in one line: frontier tokens for decisions, local tokens for sweat. Cross-model separation also mitigates the self-verification trap.

## Core loop (decided)

1. Orchestrator works a task with current registered tools (ReAct-style).
2. **Wall detector**: orchestrator classifies failure as (a) missing tool, (b) misuse of existing tools, (c) impossible task. Known-hard component; expect iteration here.
3. On (a): orchestrator checks the registry first — reuse or compose before forging.
4. If forging: orchestrator calls a single **forge tool**. That tool call kicks off an internal loop:
   a. Take the request — the spec for the missing capability (name, signature, docstring, description of what's needed).
   b. A frontier model writes adversarial tests from the spec only, before any implementation exists (TDD).
   c. Qwen TDD-implements against those tests inside a harness, iterating until green.
      - Qwen has a **docs-RAG tool** to retrieve real API docs while coding (compensates for small-model knowledge gaps).
      - **Loop budget**: max N iterations, then escalate to orchestrator with the failure log.
5. **Satisfaction ≠ green tests**: after green, orchestrator runs a holdout check (2–3 unseen test cases OR spec-conformance code review).
6. Tool + spec + tests registered, along with a companion usage skill (see Skills tier) covering how to prompt and pass arguments to the new tool correctly. Harness appends the new tool schema to subsequent orchestrator API calls (the model never edits its own payload; the harness grows its world between turns).

## Registry & curation (decided)

- **Tool registry**: tool specs indexed with usage stats. Retrieval runs BEFORE the forge fires, so the orchestrator can reuse or compose existing tools first.
- **Curator** (v2): periodic pass to merge near-duplicate tools, deprecate flaky ones, promote battle-tested ones. Registered tests act as the regression suite during merges/refactors.
- **Granularity principle**: prefer composable primitives (e.g. browser_click, browser_read) over task-specific mega-tools (check_my_email). Enforcement idea: critic rejects overly-specific specs. (Mechanism not finalized.)

## Skills tier (decided)

Two-tier library:

- **Tools** = code, tested, forged by Qwen.
- **Skills** = markdown workflow playbooks (multi-step sequences with judgment, e.g. "log into webmail and triage inbox"), authored by the orchestrator after a successful multi-step task, referencing tools by name. Each skill carries a `---description---` frontmatter field; the harness appends matching skill descriptions to the orchestrator's system prompt (not embedding retrieval), and the orchestrator decides whether to load the full skill body for a given task.
- **Tool-usage skills**: when a tool is forged, the orchestrator also authors a companion usage skill — how to prompt for it and pass arguments correctly — before the tool is used in a live task.

## Safety (decided)

- All generated code runs in a **sandbox**: container, no network by default, per-tool allowlisted domains.
- Log every credential access and every execution.
- Dev/testing uses throwaway accounts only.

## Forge timing (decided: start with mid-task)

- v1: **mid-task forge** — pause, build, resume.
- v2 (later): post-mortem forge — task fails, tool gets forged, task retried fresh.

## Evals (decided; these produce the README graphs)

- Tool **reuse rate** over time
- **Composition depth** (tools built from tools)
- Success rate on **held-out tasks** over time
