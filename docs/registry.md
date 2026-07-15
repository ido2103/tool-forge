# Registry

**Status: not implemented**

The growing toolbox: stores each tool's spec, implementation, tests, and usage stats.

## Behavior (from [spec](spec.md))

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
