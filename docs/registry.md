# Registry

**Status: v0 implemented — live tool store + safety envelope. Spec/test storage,
retrieval-before-forge, and the curator are not yet built.**

The growing toolbox: stores each tool's spec, implementation, tests, and usage stats.

## What exists today (`src/toolforge/registry/`)

- **`ToolRegistry`** (`registry.py`) — an *instance* store the orchestrator holds
  (not a module-global). `register(tool, *, replace=False)`, `replace(tool)`,
  `unregister(name)`, `has(name)`, `get_schemas()`, `async execute(name, input)`.
- **`RegisteredTool`** (`types.py`) — `name`, `description`, `input_schema`, async
  `handler`, and a `trust` level (`TRUSTED` | `UNVERIFIED`). `.schema` renders the
  Anthropic-shape `{name, description, input_schema}` sent to the model.
- **`ToolContext`** (`types.py`) — per-turn state threaded into handlers: a `turn_id`
  and cancel-handler registration (`register_cancel_handler` / `reset_cancel_handlers`
  / `fire_cancel_handlers`, the last bounded to 2s) so an in-flight tool can be aborted
  on emergency stop.
- **`wrap_tool_result`** (`safety.py`) — the XML envelope applied to string output by
  `execute`. See below.

### The live-growth contract (load-bearing)

`get_schemas()` builds a **fresh list on every call** — never a cached snapshot. The
orchestrator calls it at the top of each iteration, so when the forge registers a new
tool mid-task the model can call it on the very next turn. This is the mechanism behind
"the harness grows the model's world between turns" without editing the model's payload.

### Trust and the safety envelope

`execute` wraps string tool output via `wrap_tool_result` before it re-enters context:

- `TRUSTED` (hand-written seed tools): plain `<tool_result tool="…" trust="TRUSTED">…</tool_result>`.
- `UNVERIFIED` (forged tools; anything touching the outside world): adds a
  `<prompt_injection_warning>` and an `<external_content>` boundary so the model treats
  the payload as data, not instructions.

List (multimodal) content passes through unwrapped. `execute` raises `KeyError` on an
unknown tool name; the loop converts that into an `is_error` result so a hallucinated
name never aborts the run.

## Behavior (from [spec](spec.md)) — not yet implemented

- **Retrieval before forging**: the orchestrator queries the registry before the forge
  fires, so it can reuse or compose existing tools first.
- Registration stores tool + spec + tests together; the tests double as the regression
  suite for later curation.
- **Curator (v2)**: periodic pass to merge near-duplicate tools, deprecate flaky ones,
  and promote battle-tested ones.

## Design notes

- **Granularity principle**: prefer composable primitives (`browser_click`,
  `browser_read`) over task-specific mega-tools (`check_my_email`). Candidate
  enforcement: a critic that rejects overly-specific specs — mechanism not finalized.
