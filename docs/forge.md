# Forge

**Status: not implemented**

Turns a tool spec from the orchestrator into a verified, registered tool. Exposed to the
orchestrator as a single **forge tool** call that runs an internal loop.

## Loop (from [spec](spec.md))

1. Receive the spec for the missing capability: name, signature, docstring, description.
2. A frontier model writes **adversarial tests from the spec only**, before any
   implementation exists (TDD).
3. The forge worker (configurable backend, see below) implements against those tests
   inside a harness, iterating until green.
   - Worker has a **docs-RAG tool** to retrieve real API docs while coding — compensates
     for small-model knowledge gaps.
   - **Loop budget**: max N iterations, then escalate to the orchestrator with the
     failure log.
4. Hand back to the orchestrator for the satisfaction review (holdout check) —
   green tests alone never register a tool.

## Worker backend

The worker is selected by configuration, not hardcoded. Both modes are first-class:

- **api** (default): a cheaper API model (e.g. Claude Haiku). No local hardware
  required — the whole system runs API-only.
- **local**: Qwen3.6-35B-A3B or Qwen3.6-27B, served through any OpenAI-compatible
  endpoint (LM Studio, Ollama, vLLM). Cuts token cost on the high-volume
  implementation loop.

Invariant in both modes: the worker is a **different model** from the orchestrator /
test author.

> **Divergence from [spec.md](spec.md):** the spec pins the worker to local
> Qwen3.6-35B-A3B; recorded here per the documentation contract.

## Design notes

- Cross-model separation (frontier writes tests, a different model implements)
  mitigates the self-verification trap.
- v1 forges **mid-task**: pause the task, build, resume. Post-mortem forging (fail →
  forge → retry fresh) is v2.
