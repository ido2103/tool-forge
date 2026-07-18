# Forge

**Status: orchestrator interface stubbed (`forge_tool` / `register_tool`); internal build loop not implemented**

Turns a tool spec from the orchestrator into a verified, registered tool. Exposed to the
orchestrator as **two composable tools** (`src/toolforge/forge/tools.py`): `forge_tool`
builds a *candidate*, and `register_tool` promotes it after the orchestrator's own
holdout check. Both are wired into the REPL registry; today they fully validate their
input and return a guided not-implemented error — the internal build loop is the next
slice.

## Orchestrator interface

1. **`forge_tool`** — takes the spec, authored entirely by the orchestrator:
   `gap_analysis` (first, required: what was tried and why no existing tool or
   composition closes the gap), `name`, `description` (the model-facing description the
   finished tool will carry), `input_schema` (Anthropic-native JSON Schema — the single
   source of truth for the tool's arguments), `behavior` (contract: normal behavior,
   edge cases, error contract), plus optional `allowed_domains` (bare hostnames; empty =
   no network; any domain ⇒ the tool's output is UNVERIFIED) and `examples`
   (input→output pairs). It builds the candidate — code + tests as files in the sandbox
   workspace — and returns the code, test report, and file paths. **It never
   registers anything.**
2. **Holdout, run by the orchestrator itself** — exercise the candidate files against
   2-3 *unseen* inputs via `run_bash`, and/or review the code against the behavior
   contract. No dedicated holdout mechanism exists; the existing primitives compose.
3. **`register_tool`** — takes `holdout_evidence` (first, required: the concrete unseen
   cases/review and their results) and `name`, and promotes the candidate into the live
   `ToolRegistry`. Because the loop re-reads `get_schemas()` every iteration, the new
   tool is callable on the next turn. Registered forged tools are `UNVERIFIED`.

The gap between the two calls structurally enforces the spec's rule that **green tests
alone never register a tool**: the forge grades its own homework, so an independent
verification must happen in between.

Candidates live in an in-memory `CandidateStore` (`src/toolforge/forge/candidates.py`)
that both tools close over, keyed by name; re-forging a name replaces the candidate (the
revision path after a failed verification), and nothing persists across sessions — v1
forges mid-task, so nothing needs to. Open item: `/reset` in the REPL does not yet clear
the store. Both forge tools are `TRUSTED` (their output is harness-generated text).

The worker's **iteration budget is configuration, not a tool parameter** — deliberately
kept out of the schema so a failed forge is answered with a better spec, not a bigger
budget.

## Loop (from [spec](spec.md))

1. Receive the spec for the missing capability (see interface above).
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

## Divergences from [spec.md](spec.md)

Recorded here per the documentation contract:

- The spec pins the worker to local Qwen3.6-35B-A3B; the implemented system
  generalizes it to a configurable backend (api or local).
- The spec describes "a single **forge tool** call"; implemented as **two** composable
  tools (`forge_tool` + `register_tool`, per the granularity principle) so the holdout
  check stays in orchestrator judgment between build and registration.
- The spec's input list says "signature"; implemented as an orchestrator-authored
  Anthropic-native `input_schema`, from which the implementation's Python signature is
  derived mechanically — one source of truth, no schema/signature drift.
- The companion usage skill at registration time is deferred until the skills
  subsystem exists; `register_tool`'s contract will grow a field for it.

## Design notes

- Cross-model separation (frontier writes tests, a different model implements)
  mitigates the self-verification trap.
- v1 forges **mid-task**: pause the task, build, resume. Post-mortem forging (fail →
  forge → retry fresh) is v2.
