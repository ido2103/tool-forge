# Evals

**Status: not implemented**

A first-class deliverable — these measurements produce the README graphs that make this
a portfolio piece, not an afterthought.

## Metrics (from [spec](spec.md))

- **Tool reuse rate** over time — does the toolbox actually get reused, or does the
  agent forge everything fresh?
- **Composition depth** — tools built from tools; evidence the granularity principle
  (composable primitives) is working.
- **Success rate on held-out tasks** over time — does a growing toolbox translate into
  capability?

## Candidate metric (from the trust-envelope design)

- **Injection report rate** — eval worlds plant prompt-injection payloads inside
  external data (downloaded files, user-supplied media, world fixtures); measure how
  often the orchestrator reports them per the standing rule instead of following
  them. Doubles as the A/B measurement for the slim vs. full warning envelope
  ([sandbox.md](sandbox.md#planned-workspace-taint-replaces-network-posture-decided-not-built)).
