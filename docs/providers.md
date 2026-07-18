# Providers

**Status: implemented** (initial port from Zeemon's provider layer).

The single model-I/O layer under the orchestrator and the forge. Everything that
talks to a model API lives here; the agent loops above it only ever see the
canonical message types and normalized events.

## Interface

`base.py` defines the contract:

- **`ProviderClient`** (Protocol) — `stream(...)` yields normalized events
  (`TextDelta`, `ThinkingDelta`, `ToolUseStart/Delta/End`, `MessageEnd`);
  `send(...)` drains the stream and returns the final `Message`, optionally
  firing per-delta callbacks. Both are keyword-only and async. `drain_send()`
  is the shared `send()` body.
- **Canonical types** (`messages.py`) — `Message` with a discriminated
  `ContentBlock` union (`text`, `tool_use`, `tool_result`, `thinking`,
  `redacted_thinking`, `document`, `image`) plus provider metadata
  (`provider`, `auth_mode`, `model`, `usage`, `stop_reason`, `latency_ms`).
- **Normalization contract** — `Message.stop_reason` always uses the Anthropic
  vocabulary (`end_turn`, `tool_use`, `max_tokens`, `pause_turn`,
  `stop_sequence`, `refusal`, …) and tools are always passed in Anthropic shape
  (`{name, description, input_schema}`), regardless of provider. Adapters
  translate at the edge.

## AnthropicClient (`anthropic.py`) — orchestrator

Streams via the official `anthropic` SDK. Configured by
`toolforge.config.AnthropicSettings` (see `.env.example`).

- **Auth modes** (`TOOLFORGE_ANTHROPIC_AUTH_MODE`):
  - `api_key` — `ANTHROPIC_API_KEY` sent as `x-api-key`.
  - `oauth` — piggybacks a Claude Pro/Max subscription token
    (`oauth_anthropic.py`): reads/refreshes a creds JSON
    (`{"accessToken","refreshToken","expiresAt"}`), sends it as
    `Authorization: Bearer`, and masquerades as Claude Code (beta headers,
    `claude-cli` user-agent, and a mandatory `"You are Claude Code..."` first
    system block — without it the API returns spurious 429s). **Caveat:** this
    is a ToS-gray pattern inherited from Zeemon; use deliberately. Refresh
    tokens are single-use — two apps sharing one creds file can race and
    invalidate each other's pair. Token I/O runs off-loop
    (`asyncio.to_thread`). The PKCE provisioning flow was *not* ported —
    provision the creds file externally (a Zeemon file works as-is).
- **Retry ladder** — up to 5 retries with exponential backoff + jitter on
  429/500/502/503/529, connection errors, raw httpx timeouts, and (≤2×) the
  SDK's malformed-tool-JSON `ValueError` (anthropic-sdk#1265). On OAuth 401:
  force-refresh once, then retry. Retries apply **only until the first event
  is delivered to the consumer** — a retried attempt re-samples the response
  from scratch, so retrying after deltas already reached
  `on_text_delta`/`on_thinking_delta` would duplicate live output. Mid-stream
  failures therefore surface to the caller (this also makes the #1265
  workaround pre-delivery-only). Exceptions re-raise after exhaustion, then
  `send()` translates them into the neutral **error taxonomy** (below) so the
  orchestrator loop never imports the SDK exception types.
- **Prompt caching** — top-level `cache_control` on every request
  (`ephemeral`, or 1-hour TTL via `TOOLFORGE_ANTHROPIC_CACHE_TTL=1h`).
- **Thinking** — `TOOLFORGE_ANTHROPIC_EXTENDED_THINKING=adaptive` (default)
  sends `{"type": "adaptive", "display": "summarized"}`; set `off` for models
  that reject adaptive thinking.
- Outbound messages pass through the sanitizers (`_anthropic_sanitize.py`):
  signature-less thinking blocks stripped, foreign keys removed, emptied
  assistant turns patched, orphaned `tool_use` blocks repaired with synthetic
  error results.

## OpenAICompatClient (`openai_compat.py`) — forge worker

Chat Completions against any OpenAI-compatible server (vLLM, llama.cpp,
LM Studio, Ollama) at `http://{host}:{port}/v1`, configured by
`toolforge.config.WorkerSettings`. The caller still passes canonical messages
and Anthropic-shape tools; the adapter translates both ways and mints stable
`toolu_...` ids for OpenAI `call_...` ids (`IdMapper`).

`finish_reason` → `stop_reason` mapping:

| finish_reason | stop_reason |
|---|---|
| `stop` | `end_turn` (forced to `tool_use` if the turn produced tool calls — llama.cpp quirk) |
| `tool_calls`, `function_call` | `tool_use` |
| `length` | `max_tokens` |
| `content_filter` | `refusal` |
| anything else | passed through unchanged |

`delta.reasoning_content` (the vLLM/llama.cpp reasoning-parser extension for
Qwen-style models) maps to `ThinkingDelta` / an unsigned `ThinkingBlock`.
Malformed streamed tool arguments fall back to `{}` input rather than crashing.
Same retry ladder as the Anthropic client (local servers drop connections
during warm-up), with the same rule: no retry once any event was delivered —
mid-stream failures surface to the caller. The worker sends `max_tokens`
(accepted by vLLM/llama.cpp; OpenAI proper deprecates it, but OpenAI proper is
not a target).

## Error taxonomy (`base.py`)

`stream()` runs an SDK-typed retry ladder internally, but what escapes `send()`
is a provider-neutral pair so the orchestrator loop stays adapter-agnostic:

- **`TransientProviderError`** — retryable: HTTP 429/500/502/503/529, a
  connection drop, a raw read/connect timeout, or an HTTP-200 SSE body whose
  `error.type == "api_error"` (a mid-stream failure masquerading as success).
- **`PermanentProviderError`** — non-retryable: any other 4xx (malformed
  request, auth). Fail fast.

Both subclass `ProviderError`. The classifier `is_transient_status(status,
err_type)` encodes the retryable-status set once; each adapter's
`_to_provider_error` calls it and wraps at the `send()` boundary via
`except (APIStatusError, APIConnectionError, httpx.TimeoutException) → raise
_to_provider_error(...)`. `asyncio.CancelledError` is a `BaseException` and is
deliberately **not** caught — cooperative cancellation propagates untranslated.
The loop retries once on `TransientProviderError` (after a long pause) and lets
`PermanentProviderError` propagate; see [orchestrator.md](orchestrator.md).

## Usage hook (`usage.py`)

Each client emits a `UsageEvent` (tokens, cache tokens, latency, stop_reason,
component, turn_id) through an async `UsageHook` at the end of every turn. The
default hook logs; the **evals** subsystem will supply a persisting hook for
the README graphs. A raising hook is logged and swallowed — it never aborts a
model turn (deliberate change vs Zeemon, where a cost-ledger failure
propagated).

## Configuration

`src/toolforge/config.py` — pydantic-settings, `.env` + environment only (no
YAML). Every variable is documented in [`.env.example`](../.env.example).
Fail-fast validation: `api_key` mode requires a key; `oauth` mode requires the
creds file to exist.

## Testing

- Unit tests (`tests/providers/`) mock both SDKs at the httpx transport layer
  with **respx** — raw SSE bytes in, wire-shape assertions out. They run in CI
  with no credentials.
- Live smoke tests (`tests/providers/test_live.py`, marker `live`) hit the real
  Anthropic API and the configured local worker server; deselected by default —
  run with `uv run pytest -m live`. Missing creds/server → skip. In OAuth mode
  a stale token is refreshed and the creds file rewritten in place.

## Provenance

Ported from Zeemon (`~/Projects/Zeemon/src/zeemon/providers/`), adapted:

- **Dropped:** Postgres cost ledger (→ `UsageHook`), PKCE provisioning flow,
  the Responses-API OpenAI adapter (replaced by Chat Completions), image
  downscaling (`shrink_oversized_images` + Pillow dep — toolforge has no image
  ingestion yet; an oversized base64 image would 400 at the API), Zeemon
  transport metadata (`source`/`external_id`) and `OpaqueReasoningBlock`,
  the `get_client` primary-provider dispatcher (toolforge roles are fixed).
- **New:** `finish_reason` normalization, worker retry ladder, `drain_send`,
  protocol `stream` declared non-async (kills Zeemon's `cast`).
- **Why no LangChain/LiteLLM:** the core mechanic — harness-appended tool
  schemas, prompt-cache preservation, stop_reason branching, per-call usage
  accounting — requires owning the wire format; an abstraction library hides
  exactly that. Providers with OpenAI-compatible endpoints already work via
  the worker client; a genuinely different API would become another thin
  adapter behind the same protocol.
