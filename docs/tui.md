# TUI

**Status: implemented — chat pane with streamed thinking/answer, slash
commands, boot/reset flows, tool-activity sidebar, live forge panel, and the
`ask_user` modal, all over `bootstrap.build_host`.**

The rich interactive surface (`src/toolforge/tui/`, `toolforge-tui` console
script). The stdlib REPL (`toolforge`) remains the dependency-free fallback;
both are thin *hosts* over the same assembly point
(`orchestrator/bootstrap.py::build_host`) and the same observation seams
(streaming callbacks + `HookManager`), so neither adds anything to the core.

## Layout

```
┌ Header (title · model + sandbox status) ────────────────────────┐
│ ChatLog (#chat, 2fr)              │ ToolActivity (#activity)    │
│   » user messages (accent, bold)  │   → run_bash: echo hi       │
│   thinking (muted, streamed)      │   ✓ run_bash (42ms)         │
│   answers (streamed)              ├─────────────────────────────┤
│   (system notes, errors)          │ ForgePanel (#forge; hidden  │
│                                   │  until a forge_tool call)   │
│                                   │   forge[x]: attempt 2/4·1:23│
│                                   │   → write_tool_code …       │
├ Input (#prompt) ────────────────────────────────────────────────┤
└ Footer (Esc stop · ^N new session) ─────────────────────────────┘
```

## Module map

- `app.py` — `ToolforgeApp(App)`; `main()` builds the default `Host` and
  catches config errors (`ValidationError`, the cross-model `ValueError`)
  **before** `App.run()`, so they print to a normal terminal instead of dying
  inside the alternate screen.
- `widgets.py` — `ChatLog`: per-message `Static`s (all `markup=False` — chat
  text is full of literal `[...]` brackets) plus a mutable streaming tail;
  deltas accumulate in string buffers and a ~20 Hz timer flushes what changed,
  so token-rate updates never thrash layout. `ToolActivity`: one row per
  *orchestrator* tool call (`→ name: preview` → `✓/✗ name (latency)`).
  `ForgePanel`: reveals on a `forge_tool` pre-execute; renders the
  `ON_FORGE_PHASE` narration as a bold status line with an elapsed clock
  (1 s tick), and streams the worker's own tool calls
  (`component == "forge_worker"`) into a feed — those never appear in
  `ToolActivity`. The panel keeps its final state (✓ candidate ready /
  ✗ build failed) after the call ends.
- `messages.py` — the typed message vocabulary (`ThinkingDelta`, `TextDelta`,
  `TurnFinished`, `ToolStarted`, `ToolFinished`, `ForgePhase`). Every signal
  from the agent side crosses into the UI as one of these.
- `bridge.py` — adapts the orchestrator's contracts onto the pump:
  `make_delta_callbacks(app)` for `Orchestrator.run`, `attach_hooks(app,
  hooks)` registering `ON_TOOL_PRE/POST_EXECUTE` + `ON_FORGE_PHASE` observers
  that post messages and return.
- `screens.py` — `AskUserScreen(ModalScreen[str])`: question, context, one
  button per option (recommended = primary + focused), free-text `Input`. A
  button dismisses with the option's **verbatim label** (the tool handler's
  label check depends on it); empty free text never answers; there is no
  escape-dismiss — the only way past the question is answering or cancelling
  the turn.
- `styles.tcss` — layout + message + modal styling.

## Concurrency model

Textual owns the asyncio loop; nothing runs on threads.

- **Boot**: `on_mount` starts a worker awaiting `sandbox.start()` — first paint
  is never blocked; failure renders the "Is Docker running?" error in-app with
  input disabled (`/reset` retries).
- **Turns**: submit → an exclusive `"turn"`-group worker awaiting
  `Orchestrator.run(...)`. The app owns `self._history` (the loop's
  caller-owns-history contract).
- **Ordering rule**: the turn worker never touches widgets after streaming
  starts — it posts `TurnFinished` through the pump, which guarantees arrival
  *after* every queued delta (finishing synchronously in the worker races
  them; that was a real bug caught by the pilot tests).
- **Cancellation**: Esc → `orchestrator.request_stop()` — the same semantics as
  the REPL's Ctrl-C. The Textual worker is *never* cancelled: the loop's
  cancel-event path ends the turn cleanly ("Stopping."), keeps history
  consistent, and fires tool cancel-handlers.
- **ask_user wiring**: `app.ask_user(request)` awaits
  `push_screen_wait(AskUserScreen(request))` — it runs inside the tool's task,
  a child of the turn worker, so the active-worker context is present (verified
  by pilot test). `main()` bridges the build-order gap with `AskUserProxy`:
  `build_host` gets the proxy as its callback before the app exists, and the
  app is bound to it after construction. Turn cancellation while a question is
  open cancels the await; the handler dismisses the orphaned modal and the
  question resolves as an `[ABORTED]` tool result — never a fabricated answer.
  The modal's `Input.Submitted` is `stop()`ped so an answer can't leak into the
  app's prompt handler as a new task; `serial_group="user"` already guarantees
  at most one modal at a time.

## Commands & bindings

`/new` (clear history) · `/reset` (also drop candidates + recycle the
container) · `/quit` `/exit` · unknown `/x` gets a hint. `Esc` stops the
running turn; `^N` = `/new`. Commands other than quit are rejected while a turn
runs. Sandbox teardown stays on the `atexit` hook registered by `build_host`.

## Testing

`tests/tui/` runs entirely on stubs (`_harness.py`): a `StreamingFakeClient`
(scripted replies that also stream deltas), `FakeRunner` sandbox, and a real
`Orchestrator`/`ToolRegistry` — no Docker, no API. Pilot tests
(`App.run_test()`) cover boot success/failure, streamed turns, tool-call
turns, provider-error survival, and the slash commands. The real
sandbox/model path is manual:

```bash
uv run toolforge-tui                 # the app
uv run textual run --dev toolforge.tui.app:ToolforgeApp   # with live CSS reload
uv run textual console               # log viewer (pair with --dev)
```
