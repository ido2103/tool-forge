# Skills

**Status: not implemented**

The second tier of the library. Tools are code; **skills are markdown workflow
playbooks** — multi-step sequences with judgment (e.g. "log into webmail and triage
inbox") that reference tools by name.

## Behavior (from [spec](spec.md))

- Authored by the orchestrator after a successful multi-step task.
- Each skill carries a `---description---` frontmatter field. The harness appends
  matching skill descriptions to the orchestrator's system prompt (deliberate choice:
  not embedding retrieval); the orchestrator decides whether to load the full body for
  a given task.
- **Tool-usage skills**: every forged tool gets a companion usage skill — how to prompt
  for it and pass arguments correctly — before the tool is used in a live task.
