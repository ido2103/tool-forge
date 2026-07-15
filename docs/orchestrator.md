# Orchestrator

**Status: not implemented**

The frontier-model brain (Claude Sonnet/Opus via API). Owns every judgment call in the
system; the forge worker never decides, only implements.

## Responsibilities (from [spec](spec.md))

- Work tasks ReAct-style with the currently registered tools.
- **Wall detector**: on failure, classify as (a) missing tool, (b) misuse of existing
  tools, (c) impossible task. Known-hard component — expect iteration here.
- On (a): query the registry first; reuse or compose existing tools before forging.
- Author tool specs (name, signature, docstring, description) for the forge.
- **Satisfaction review**: green tests are not enough — after the forge reports green,
  run a holdout check (2–3 unseen test cases or a spec-conformance code review).
- Author skills after successful multi-step tasks, and a companion usage skill for every
  newly forged tool before its first live use.

## Design notes

- The harness appends new tool schemas to subsequent API calls between turns; the model
  never edits its own payload.
- Orchestration accumulates long context (tool registry, task history) — this is why the
  role gets the frontier model.
