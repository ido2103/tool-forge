# Sandbox

**Status: v0 implemented — Docker-contained `run_bash`. Per-tool domain
allowlists, no-network-default for generated code, and credential logging are
future slices.**

All generated code runs here — never on the host.

## What exists today (`src/toolforge/sandbox/`)

- **`BashSandbox`** (`bash.py`) — manages one Docker container:
  - **Image** `python:3.12-slim` (config: `TOOLFORGE_SANDBOX_IMAGE`), started
    **lazily** on the first command via `docker run -d … sleep infinity`, and
    **persistent for the sandbox object's lifetime** (the REPL process).
  - Each command runs `docker exec <name> bash -lc <command>` — a **fresh shell
    per call**, so `cd`/env do not persist. The tool description tells the model
    to use absolute paths.
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
- **`run_bash`** (`run_bash.py`) — `build_run_bash(sandbox)` returns the
  `TRUSTED` seed tool. It validates `command`/`timeout`, runs the command, and
  formats `output + [exit code: N]`, marking a nonzero exit or a timeout as
  `is_error`.

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
