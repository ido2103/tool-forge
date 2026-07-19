# Orchestrator

**Status: v0 loop implemented — ReAct send→tools→repeat with full stop_reason
handling, serial-group tool execution, cancellation, and transcripts — plus the
`ask_user` clarification tool (blocking mid-turn question, REPL-serviced). The
wall detector, spec/skill authoring, and satisfaction review are not yet built.**

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
- **Tool execution** (`_execute_tools`) runs the turn's tool calls concurrently by
  default; calls whose tool declares a `serial_group`
  ([registry.md](registry.md)) are chained per group and run **one at a time, in
  the order the model emitted them** (`run_bash` and future sandbox-backed tools
  share the `"sandbox"` group — they mutate one `/workspace`, and a
  write-then-run pair emitted in parallel must not race). A failed predecessor
  never skips its successors, groups run concurrently with each other and with
  parallel-safe tools, and results are re-assembled in original order. A handler
  exception or unknown-tool `KeyError` becomes an `is_error` result — a bad tool
  never aborts the run. The model-facing error text is `repr(exc)` — **never a traceback**
  (frames go to the log via `exc_info=True`, not the context window) — and it is:
  - **capped** at `_ERROR_CONTENT_CAP` (4k chars) with a truncation note. Exception
    messages are unbounded in practice (embedded subprocess output, validation dumps,
    HTTP bodies); uncapped, one raising tool can cost tens of thousands of tokens.
  - **wrapped in the safety envelope** using the failing tool's trust level. The
    handler raised before `registry.execute` could wrap, so the loop applies it —
    an `UNVERIFIED` (forged) tool's exception message can carry external text just
    as its return value would.
- **Cancellation**: `request_stop()` sets a per-run event; the loop checks it at each
  turn boundary and after each send, aborts in-flight tools (firing their
  cancel-handlers), synthesizes `[ABORTED]` tool results, and returns `"Stopping."`.
  Serialized calls still queued behind a predecessor are cancelled the same way and
  also render as `[ABORTED]`.
- **Transient retry**: one long-pause retry on `TransientProviderError` (see
  [providers.md](providers.md#error-taxonomy-basepy)); `PermanentProviderError`
  propagates.
- **Wrap-up on cap**: when iterations are exhausted, one final `tools=None` call asks the
  model to answer in plain text, so a runaway turn ends with a coherent summary rather
  than a dangling tool call.
- **Hooks** (`hooks.py`) fire at `ON_ITERATION` / `ON_TOOL_PRE_EXECUTE` /
  `ON_TOOL_POST_EXECUTE` / `ON_INTERMEDIATE_TEXT` / `ON_RESPONSE`; the REPL uses them for
  one-line status, and evals will attach here later. Handlers are exception-swallowing.
  **Every fire carries `component`** (the loop's constructor arg: `"orchestrator"`, or
  `"forge_worker"` for the worker's inner loop) — the forge worker shares the host's
  `HookManager`, and `component` is how a renderer keeps the worker's inner-loop
  events out of the orchestrator's chat stream.

Config comes from `OrchestratorSettings` (`max_iterations`, `max_tokens_per_turn`,
`system_prompt_path`, `runs_dir`).

## Host assembly (`bootstrap.py`)

`build_host(anthropic, orch, sandbox, worker, test_author, *, hooks=None,
ask_user=None) -> Host` is the single assembly point every surface boots
through: it validates the cross-model invariant, wires clients, sandbox, forge
pipeline, registry, transcript, and the loop, and returns a `Host` dataclass
(`orchestrator`, `sandbox`, `candidates`, `registry`, `hooks`, `system_prompt`,
`loaded_tools`, `tool_store_warnings`). Hosts differ only in what they inject:

- **`hooks`** — a `HookManager` pre-loaded with the host's observers (the REPL
  registers its tool one-liners before calling; a richer host attaches its own
  renderers). `None` builds an empty manager.
- **`ask_user`** — the host's human answer channel; `None` is the headless
  contract (tool never registered, schema never reaches the model).

`build_host` performs no I/O beyond reading the persisted toolbox — no container
start (callers own `sandbox.start()`), no printing: boot findings
(`loaded_tools`, `tool_store_warnings`) come back on the `Host` for the caller
to render. The REPL (`repl.py`) is now a thin driver over `build_host`; future
hosts (TUI, evals, web/MCP) call the same function with their own injections.

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

## `ask_user` clarification tool (`ask_user.py`)

**Motivation.** In the first real REPL run ("I want to feed you audio files and
have you transcribe them", local transcript `runs/20260718T184024884822Z.jsonl`,
gitignored), the agent performed superbly but silently made several
*consequential spec decisions* the user never saw: local Whisper vs. a cloud STT
API, model size (speed/accuracy), JSON output shape, and ~hundreds of MB of
model cache written to the persistent workspace. Defensible defaults — but for a
tool-forging agent, ambiguity at spec time becomes baked-in tool behavior
forever. The wall detector's three verdicts (missing tool / misuse / impossible)
have a fourth in practice: **underspecified — ask** (the verdict itself lands
when the detector is built; the tool it needs exists now).

**Mechanism** — a *blocking mid-turn tool*. The model calls `ask_user` with a
`question`, the `context` behind it (constraints/trade-offs, deliberately
ordered before the options in the schema so the options follow from stated
reasoning), and 2–4 `options` (`{label, description, recommended?}`, at most
one recommended). The handler validates and awaits a host-injected
`AskUserCallback`; the answer returns as the `tool_result` and the turn resumes
in place — an auditable `tool_use`/`tool_result` pair in the transcript.
Invalid input gets an actionable `is_error` message, and the callback is never
invoked for it.

- **Answer semantics**: the user picks an option (result: `User chose:
  "<label>"`, label verbatim — semantic handles beat indices) or answers
  free-form (`User answered: <text>`, authoritative). Empty input re-prompts —
  no silent default, an accidental Enter must not make the decision this tool
  exists to surface.
- **Serial group `"user"`**: multiple questions in one batch run one at a time,
  in emission order — they must not race for the single human input channel.
- **TRUSTED**: the result is the user's own words; the user is the principal,
  so no safety envelope applies.
- **Headless rule**: hosts without a human (evals, automated runs) simply don't
  call `build_ask_user` — an unregistered tool has no schema in the payload, so
  the model can't call it and never learns that asking "fails". Eval harnesses
  that want to exercise ask-behavior inject a scripted callback.
- **REPL wiring** (`repl.py::_ask_via_stdin`): renders the question, dimmed
  context, and numbered options (recommended flagged), then reads stdin; a
  digit picks, anything else is free-form. If stdin is closed (EOF — e.g. a
  non-interactive invocation) the callback raises `AskUserUnavailableError`,
  which the handler converts to an `is_error` result — a failure to reach the
  user is never synthesized into an answer. Known v1 warts: a Ctrl-C stop while
  waiting aborts the question (`[ABORTED]` result) but the orphaned `input()`
  thread may swallow the next typed line, and concurrently running sandbox
  tools can print one-liners mid-prompt. Accepted for a single-user REPL.

The system prompt (`prompts/system.md`, "Asking the user") carries the policy:
mandatory asks for decisions baked into a forged tool's spec/tests,
hard-to-reverse or externally visible actions, and materially branching intent;
liberal asking welcome on genuine ambiguity below that threshold; guards —
batch related decisions into one question, never ask what a tool/registry/docs
lookup can answer.

## Design notes

- The harness appends new tool schemas to subsequent API calls between turns; the model
  never edits its own payload.
- Orchestration accumulates long context (tool registry, task history) — this is why the
  role gets the frontier model.
