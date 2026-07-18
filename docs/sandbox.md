# Sandbox

**Status: v0 implemented — Docker-contained `run_bash` with a pipefail shell,
eager container start, and serialized execution via the `"sandbox"` serial
group. Per-tool domain allowlists, no-network-default for generated code, and
credential logging are future slices.**

All generated code runs here — never on the host.

## What exists today (`src/toolforge/sandbox/`)

- **`BashSandbox`** (`bash.py`) — manages one Docker container:
  - **Image** `python:3.12-slim` (config: `TOOLFORGE_SANDBOX_IMAGE`), started
    **eagerly at REPL boot** via `start()` (`docker run -d … sleep infinity`) —
    Docker being down is a clear boot-time failure, not a mid-task surprise —
    and **persistent for the sandbox object's lifetime** (the REPL process).
    `start()` is idempotent and lock-guarded, so concurrent cold callers can
    never race to `docker run` the same container name; `run()` keeps a
    lock-guarded on-demand fallback (`_ensure_started`), which is what restarts
    the container after `/reset` or when `BashSandbox` is used bare.
  - Each command runs `docker exec <name> bash -o pipefail -lc <command>` — a
    **fresh shell per call**, so `cd`/env do not persist. The tool description
    tells the model to use absolute paths. `pipefail` makes a failure anywhere
    in a pipeline reach the exit code (without it, `curl … | head` reports
    `head`'s `0` and a dead `curl` renders as success).
  - The host **`./workspace`** dir (config: `TOOLFORGE_SANDBOX_WORKSPACE_PATH`)
    is mounted read-write at `/workspace`, which is the working directory, so
    artifacts survive and are host-inspectable. **The repo is never mounted** —
    which also keeps `.env` out of the model's reach.
  - **Timeout** per command (`TOOLFORGE_SANDBOX_COMMAND_TIMEOUT`, default 60s):
    on expiry the child is killed and an `is_error` timeout result is returned.
    (The docker-exec *client* is killed; an in-container process may linger until
    teardown — acceptable for v0.)
  - **Output** is ANSI-stripped and capped (`TOOLFORGE_SANDBOX_OUTPUT_CAP`) with
    a head+tail truncation notice that steers toward grep/head/tail.
  - **`teardown()`** force-removes the container (`docker rm -f`); idempotent,
    best-effort, synchronous so it runs from the REPL's `atexit`.
- **`run_bash`** (`run_bash.py`) — `build_run_bash(sandbox)` returns the seed
  tool, registered in serial group **`SANDBOX_SERIAL_GROUP` (`"sandbox"`)**: all
  sandbox-backed tools share one container and one `/workspace`, so the
  orchestrator runs their calls one at a time, in emission order
  ([orchestrator.md](orchestrator.md)) — a parallel write-then-run pair from the
  model cannot race. Forged tools that execute in the sandbox reuse this group.
  It validates `command`/`timeout`, runs the command, and formats
  `output + [exit code: N]`, marking a nonzero exit or a timeout as `is_error` —
  **except exit 141 (SIGPIPE)**, which under `pipefail` is what a producer
  reports when an early-exiting consumer (`… | head`) closes the pipe after
  getting exactly what it asked for; 141 is treated as success and annotated in
  the result so the model doesn't read it as failure.
  Known limit: the shell reports the **last** command's exit code, so a
  `;`-separated suffix (e.g. a trailing `; echo EXIT:$?`) masks an earlier
  failure and renders as `✓`. Pipelines are covered mechanically by `pipefail`;
  the `;`-list case is mitigated in the tool description, which states the exit
  code is auto-reported and instructs the model not to append exit markers.

### Trust follows the network posture

`run_bash`'s trust level is **derived from `sandbox.network_enabled`, not
hardcoded**. The distinction that matters is *code* vs *output*: the tool's code
is hand-written and trusted, but its stdout is whatever the command produced.

| `TOOLFORGE_SANDBOX_NETWORK` | trust | why |
|---|---|---|
| `on` (default) | `UNVERIFIED` | `curl`/`pip`/any fetch can pipe attacker-controlled text into stdout and thus into context, so results carry the prompt-injection envelope |
| `none` | `TRUSTED` | the container cannot reach out, so output stays unwrapped and avoids the warning's token cost on every call |

This keeps the code aligned with [registry.md](registry.md)'s rule that anything
touching the outside world is `UNVERIFIED`. Note the cost trade: the networked
default pays ~80 tokens of warning per tool call, which is the honest price of a
shell that can reach the internet.

### Divergences from the spec (deliberate, v0)

The spec's rules below govern **generated** code. `run_bash` is a hand-written
*trusted* seed primitive whose commands are model-chosen, so v0 diverges:

- **Network is ON by default** (`TOOLFORGE_SANDBOX_NETWORK=on`) so pip/curl work
  in demos; set `none` for the spec's isolated posture. Per-domain allowlists
  (which need a filtering proxy) are not built.
- Docker is driven via the **CLI + `subprocess`** (injectable for tests), not the
  docker SDK — no extra runtime dependency.

## Rules (from [spec](spec.md)) — target for generated-code execution

- Container isolation; **no network by default**; per-tool allowlisted domains.
- Log every credential access and every execution.
- Dev/testing uses throwaway accounts only.
