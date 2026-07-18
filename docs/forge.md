# Forge

**Status: `register_tool` and the adversarial test author implemented.
`forge_tool` remains stubbed pending the forge worker loop, which will wire the
test author in.**

Turns a tool spec from the orchestrator into a verified, registered tool. Exposed to the
orchestrator as **two composable tools** (`src/toolforge/forge/tools.py`): `forge_tool`
builds a *candidate*, and `register_tool` promotes it after the orchestrator's own
holdout check. Both are wired into the REPL registry; `forge_tool` fully validates its
input and returns a guided not-implemented error — the build loop is the next slice.
`register_tool` is real: a candidate whose files exist in the workspace is promoted,
persisted, and callable on the next turn.

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
   `ToolRegistry` **and the on-disk tool store** (see below). Because the loop re-reads
   `get_schemas()` every iteration, the new tool is callable on the next turn.
   Registered forged tools are `UNVERIFIED`.

The gap between the two calls structurally enforces the spec's rule that **green tests
alone never register a tool**: the forge grades its own homework, so an independent
verification must happen in between.

Candidates live in an in-memory `CandidateStore` (`src/toolforge/forge/candidates.py`)
that both tools close over, keyed by name; re-forging a name replaces the candidate (the
revision path after a failed verification). Candidates die with the session (`/reset`
also clears the store) — v1 forges mid-task, so unpromoted work needs no persistence.
*Registered* tools persist (see the tool store). Both forge tools are `TRUSTED` (their
output is harness-generated text).

## The tool store (persistence + execution substrate)

Promoted tools live in a host directory (default `./tools`, config
`TOOLFORGE_SANDBOX_TOOLS_PATH`) that is bind-mounted **read-only** at `/tools` in the
sandbox container: the agent can inspect registered tools but can never modify them —
the only write path is `register_tool`'s harness-side promotion. The default is
project-relative deliberately (like `./workspace` and `runs/`): a per-project toolbox
keeps eval runs isolated and the grown toolbox visible; point the env var at a shared
directory for a global toolbox. The directory is gitignored.

Layout — the filesystem is the database (no SQLite; metadata cannot drift from code):

```
tools/
  _runner.py            # harness-owned runner, reinstalled at every boot
  <name>/
    tool.py             # the implementation: a plain `def run(...)`
    test_tool.py        # the candidate's tests, when it carried them
    manifest.json       # spec + provenance, schema_version-ed
```

- **`manifest.json`** (`src/toolforge/forge/manifest.py`, `schema_version: 1`) holds
  the registration spec (`name`, `description`, `input_schema`) plus provenance
  (`behavior`, `gap_analysis`, `holdout_evidence`, `allowed_domains`, `examples`,
  `test_report`, `created_at`). The loader hard-requires the spec fields, a name that
  matches the directory, and a present `tool.py`; provenance is normalized, never
  fatal.
- **Promotion** (`src/toolforge/forge/promote.py`) is harness-side: it maps the
  candidate's `/workspace/...` paths to host files (rejecting anything that escapes
  the workspace, symlinks included), validates everything, then copies artifacts and
  writes the manifest. Nothing is written until every check passes; the candidate is
  consumed only after registration succeeds, so any failure leaves it available for a
  revised attempt.
- **Boot reload** (`load_persisted_tools`) rescans the store at REPL start and
  re-registers every valid tool — the toolbox survives restarts with zero extra
  machinery. A corrupt directory is skipped with a warning, never a crash.

## Execution and the result contract

A forged tool's `tool.py` is a plain module defining `run(...)` whose keyword
parameters match its `input_schema` properties — no I/O boilerplate for the worker to
get wrong, and pytest can import it directly. The harness-owned runner
(`src/toolforge/forge/runner.py`, installed as `/tools/_runner.py`, stdlib-only) does
the plumbing; the registered handler executes, in the shared container and serial
group, with the global `command_timeout`:

```
python3 /tools/_runner.py <name> <base64(json-input)>
```

(Base64 keeps the argv shell-inert; the name is regex-validated at both promotion and
load, so the composition is injection-safe.) What the model sees:

| outcome | exit | model-visible result |
|---|---|---|
| `run()` returns `str` | 0 | the string, verbatim |
| returns JSON-serializable | 0 | `json.dumps(...)` (`None` → `null`) |
| returns non-serializable | 1 | `[tool error: ... non-JSON-serializable <type>]`, `is_error` |
| `run()` raises | 1 | `[tool error]` + traceback, `is_error` |
| harness fault (bad input encoding, missing/unimportable `tool.py`, no `run`) | 2 | `[forged-tool harness error: ...]`, `is_error` |
| timeout | — | the standard sandbox timeout message, `is_error` |

No `[exit code: N]` suffix — a forged tool returns a value, not a shell transcript.
Since forged tools always execute sandbox-side, output flows through the existing
ANSI-strip + truncation caps, and — being model-written code — is always wrapped in
the `UNVERIFIED` prompt-injection envelope regardless of network posture.

Signature *derivation* from `input_schema` is deferred to the worker slice (which
generates the code); at this layer `run(**input)` plus Python's own `TypeError` is the
enforcement, and the error message names the missing parameter.

The worker's **iteration budget is configuration, not a tool parameter** — deliberately
kept out of the schema so a failed forge is answered with a better spec, not a bigger
budget.

## Test author

The first stage of the build loop (`src/toolforge/forge/test_author.py`,
implemented): `TestAuthor.author_tests(spec)` turns a `ToolSpec` (the validated
`forge_tool` input minus `gap_analysis`) into a pytest file at
`/workspace/build/<name>/test_tool.py` and a red-suite report — the contract the
worker slice will implement against.

- **Model**: frontier-tier by design; defaults to the orchestrator's model and
  client instance (`TOOLFORGE_TEST_AUTHOR_MODEL` overrides). The cross-model
  invariant is author-vs-worker, so sharing the orchestrator's model is fine.
  Calls are attributed to the usage hook as `component="test_author"`.
- **Prompt protocol**: the model must emit a numbered edge-case analysis
  *before* the code (reasoning precedes what it justifies), then exactly one
  fenced ```python block (plain source, never JSON-escaped code).
- **Validation pipeline**, all mechanical: static screen (offline/deterministic
  imports only, must `from tool import run`) → `pytest --collect-only` in the
  sandbox (syntax + at least `min_tests` tests) → a `pytest -v` run against a
  stub `tool.py` whose `run()` raises `NotImplementedError`. The suite is
  accepted only when that run is *all-red*: a test that passes against the stub
  asserts nothing about real behavior (vacuous) and is rejected by name. On
  success the stub is deleted so it can never be mistaken for a built artifact.
- **Retries are fix-in-context**: each rejection appends targeted feedback to
  the same conversation (the failing output, the vacuous test names) under a
  config-driven attempt budget — bounded in code, not prompt.
- **Budgets** (`TOOLFORGE_TEST_AUTHOR_*`): `MAX_ATTEMPTS` (default 3),
  `MAX_TOKENS` per call (16000), `MIN_TESTS` (5), and `TIMEOUT_SECONDS` (1500) —
  a wall-clock deadline checked before every model call and sandbox command, so
  overshoot is bounded by the longest single step. On any terminal failure the
  `build/<name>/` directory is removed and a `TestAuthorError` carries the last
  failure for the orchestrator.
- **Files are written host-side** (like promotion) into the bind-mounted
  workspace; only pytest execution runs in the container. pytest itself is
  lazily `pip install`ed once per container (the sandbox image ships without
  it), which requires sandbox network "on" at forge time.
- **Networked specs** (non-empty `allowed_domains`): v1 keeps the tests fully
  offline — the author is instructed to test only offline-verifiable behavior
  (argument validation, error contract, output shaping).

## Loop (from [spec](spec.md))

1. Receive the spec for the missing capability (see interface above).
2. **Implemented** (see "Test author" above): a frontier model writes
   **adversarial tests from the spec only**, before any implementation exists
   (TDD).
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
- `allowed_domains` is **recorded but not enforced** in v1: forged tools run in the
  shared container under the global `TOOLFORGE_SANDBOX_NETWORK` setting. Per-tool
  allowlists need the sandbox's filtering-proxy slice. (Mitigation until then: forged
  output is always `UNVERIFIED`, so fetched text is quarantined either way.)
- No per-tool timeout: the global `command_timeout` applies. The manifest's
  `schema_version` leaves room to add one without breaking stored tools.

## Design notes

- Cross-model separation (frontier writes tests, a different model implements)
  mitigates the self-verification trap.
- v1 forges **mid-task**: pause the task, build, resume. Post-mortem forging (fail →
  forge → retry fresh) is v2.
