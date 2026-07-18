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
| Forge worker | Configurable backend — see below | All labor: implementing tools against failing tests until green |

The forge worker backend is chosen by configuration; both modes are first-class:

- **api** (default): a cheaper API model (e.g. Claude Haiku). The system is fully
  usable API-only — no local hardware required.
- **local**: Qwen3.6-35B-A3B or Qwen3.6-27B, served through any OpenAI-compatible
  endpoint (LM Studio, Ollama, vLLM). Cuts token cost on the high-volume
  implementation loop.

Frontier tokens for decisions, cheap tokens for sweat. One invariant holds in both
modes: **the worker is never the same model as the orchestrator** — cross-model
separation mitigates the self-verification trap.

> **Divergence from [spec.md](spec.md):** the spec pins the worker to local
> Qwen3.6-35B-A3B. The implemented system generalizes it to a configurable backend
> (api or local, with 27B as an additional local option) so running without a
> local-model workstation is fully supported. Recorded here per the documentation
> contract.

## Core loop

1. Orchestrator works a task ReAct-style with currently registered tools.
2. On failure, the **wall detector** classifies it: missing tool / tool misuse / impossible task.
3. Missing tool → check the **registry** first; reuse or compose before forging.
4. Forge: frontier model writes adversarial tests from the spec alone; the forge worker
   implements against them (docs-RAG available, bounded iteration budget, escalate on
   exhaustion).
5. Green tests ≠ done: orchestrator runs a holdout check (unseen cases or spec-conformance review).
6. Tool + spec + tests registered with a companion usage skill; the harness appends the
   new tool schema to subsequent orchestrator calls (the model never edits its own payload).
   Registration is an explicit orchestrator-driven `register_tool` call, gated on
   holdout evidence (the companion usage skill is still future work).

v1 forges **mid-task** (pause, build, resume); post-mortem forging is v2.

## Subsystems

Each maps to a package under `src/toolforge/` and a doc in this folder:

- [orchestrator](orchestrator.md) — task loop, wall detector, satisfaction review
- [forge](forge.md) — spec → tests → implementation → verification loop
- [registry](registry.md) — tool storage, retrieval-before-forge, curator (v2)
- [skills](skills.md) — markdown playbooks + per-tool usage skills
- [sandbox](sandbox.md) — isolated execution for all generated code
- [evals](evals.md) — reuse rate, composition depth, held-out success (the README graphs)
- [providers](providers.md) — model clients: Anthropic (orchestrator; api-key/OAuth) +
  OpenAI-compatible (forge worker; vLLM/llama.cpp), canonical message types, usage hook

Runtime configuration comes from `.env` via `src/toolforge/config.py`
(pydantic-settings); every variable is documented in `.env.example`.

## Status

**Landed:**

- **providers** — implemented (ported from Zeemon; both clients tested mocked + live).
  Now also expose a provider-neutral error taxonomy (`TransientProviderError` /
  `PermanentProviderError`) translated at the `send()` boundary.
- **orchestrator** — v0 agent loop: ReAct send→tools→repeat with the full
  `stop_reason` state machine, concurrent tool execution (with per-`serial_group`
  FIFO chaining for tools that share state), graceful cancellation, a
  transient-retry, a wrap-up-on-cap, lifecycle hooks, and per-run JSONL transcripts. A
  stdlib streaming **REPL** (`toolforge` console script) drives it. The wall detector,
  spec/skill authoring, and satisfaction review are still to come.
- **registry** — v0 instance `ToolRegistry`: live add/replace (schemas re-read every
  iteration → tools grow mid-task) + the XML tool-result safety envelope. Spec/test
  storage, retrieval-before-forge, and the curator are future slices.
- **sandbox** — v0 Docker-contained `run_bash` seed tool (container started eagerly
  at REPL boot with a lock-guarded fallback, `/workspace` mount, config-toggleable
  network, output caps, serialized via the `"sandbox"` group). The spec's
  generated-code isolation (no-network-default, domain allowlists, credential
  logging) is future work.
- **forge** — orchestrator interface stubbed: `forge_tool` (spec → candidate, never
  registers) and `register_tool` (promotes after the orchestrator's holdout check),
  sharing an in-memory `CandidateStore`. Both fully validate input and return a guided
  not-implemented error; the internal build loop (test author + worker) is the next
  slice. See [forge.md](forge.md).

**How it wires together today:** the REPL builds an `AnthropicClient`, a `BashSandbox`
+ `ToolRegistry` (with `run_bash`, `forge_tool`, and `register_tool` bound to a shared
`CandidateStore` and the live registry), a `HookManager`, and a `Transcript`, starts
the sandbox container eagerly (failing loudly at boot if Docker is down), then hands
them to the `Orchestrator`. Each turn the loop re-reads the registry's schemas, calls the
provider, and dispatches tool calls into the sandbox. This is the spine the forge, wall
detector, skills, and evals will hang off.

**Skeleton only:** skills, evals; the forge's internal build loop. Update this section
as subsystems land.
