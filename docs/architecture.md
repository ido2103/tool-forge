# Architecture Overview

**One-liner:** an agent that, when it lacks a tool for a task, forges the tool itself —
spec → adversarial tests → implementation → sandbox verification → registration — so its
toolbox grows over time.

Full design rationale lives in [spec.md](spec.md) (verbatim handoff document). This file
tracks how the implemented system actually fits together and must be kept current as code
lands.

## Model split

| Role | Model | Owns |
|------|-------|------|
| Orchestrator | Frontier API model (Claude Sonnet/Opus) | All judgment: task execution, wall detection, spec & test authoring, skill authoring, satisfaction review |
| Forge worker | Qwen3.6-35B-A3B (local) | All labor: implementing tools against failing tests until green |

Frontier tokens for decisions, local tokens for sweat. Cross-model separation mitigates
the self-verification trap.

## Core loop

1. Orchestrator works a task ReAct-style with currently registered tools.
2. On failure, the **wall detector** classifies it: missing tool / tool misuse / impossible task.
3. Missing tool → check the **registry** first; reuse or compose before forging.
4. Forge: frontier model writes adversarial tests from the spec alone; Qwen implements
   against them (docs-RAG available, bounded iteration budget, escalate on exhaustion).
5. Green tests ≠ done: orchestrator runs a holdout check (unseen cases or spec-conformance review).
6. Tool + spec + tests registered with a companion usage skill; the harness appends the
   new tool schema to subsequent orchestrator calls (the model never edits its own payload).

v1 forges **mid-task** (pause, build, resume); post-mortem forging is v2.

## Subsystems

Each maps to a package under `src/toolforge/` and a doc in this folder:

- [orchestrator](orchestrator.md) — task loop, wall detector, satisfaction review
- [forge](forge.md) — spec → tests → implementation → verification loop
- [registry](registry.md) — tool storage, retrieval-before-forge, curator (v2)
- [skills](skills.md) — markdown playbooks + per-tool usage skills
- [sandbox](sandbox.md) — isolated execution for all generated code
- [evals](evals.md) — reuse rate, composition depth, held-out success (the README graphs)

## Status

Skeleton only — no subsystem implemented yet. Update this section as subsystems land.
