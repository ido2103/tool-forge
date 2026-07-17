# Orchestrator

**Status: v0 loop implemented — ReAct send→tools→repeat with full stop_reason
handling, cancellation, and transcripts. The wall detector, spec/skill authoring, and
satisfaction review are not yet built.**

The frontier-model brain (Claude Sonnet/Opus via API). Owns every judgment call in the
system; the forge worker never decides, only implements.

## What exists today (`src/toolforge/orchestrator/`)

`Orchestrator` (`loop.py`) runs one user turn to a final text answer. It is **stateless
per call**: history is a plain `list[Message]` the caller (the REPL) owns and passes in;
the loop mutates it in place and mirrors every message to a `Transcript`.

- **`run(user_text, history, *, system_prompt, on_thinking_delta=, on_text_delta=)`** —
  appends the user turn, then loops up to `max_iterations` (config):
  `tools = registry.get_schemas()` is **re-read every iteration** (live tool growth),
  then `_send_with_retry` calls the provider.
- **Stop-reason state machine** (the provider normalizes every backend to the Anthropic
  vocabulary): `end_turn`/`stop_sequence` → return text; `tool_use` → execute + loop;
  `max_tokens`/`model_context_window_exceeded` → return partial text; `pause_turn` →
  loop; `refusal` → canned text; anything else → `AgentError`. A **SSE-truncation
  override** promotes a non-`tool_use` stop reason to `tool_use` when the response
  actually carries tool-use blocks (a stream cut off mid-call).
- **Tool execution** (`_execute_tools`) runs the turn's tool calls concurrently
  (`asyncio.gather`), re-assembles results in original order, and turns a handler
  exception or unknown-tool `KeyError` into an `is_error` result — a bad tool never
  aborts the run.
- **Cancellation**: `request_stop()` sets a per-run event; the loop checks it at each
  turn boundary and after each send, aborts in-flight tools (firing their
  cancel-handlers), synthesizes `[ABORTED]` tool results, and returns `"Stopping."`.
- **Transient retry**: one long-pause retry on `TransientProviderError` (see
  [providers.md](providers.md#error-taxonomy-basepy)); `PermanentProviderError`
  propagates.
- **Wrap-up on cap**: when iterations are exhausted, one final `tools=None` call asks the
  model to answer in plain text, so a runaway turn ends with a coherent summary rather
  than a dangling tool call.
- **Hooks** (`hooks.py`) fire at `ON_ITERATION` / `ON_TOOL_PRE_EXECUTE` /
  `ON_TOOL_POST_EXECUTE` / `ON_INTERMEDIATE_TEXT` / `ON_RESPONSE`; the REPL uses them for
  one-line status, and evals will attach here later. Handlers are exception-swallowing.

Config comes from `OrchestratorSettings` (`max_iterations`, `max_tokens_per_turn`,
`system_prompt_path`, `runs_dir`).

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
