"""toolforge: a self-expanding agent that forges its own tools.

When the orchestrator hits a capability wall, it specs the missing tool,
a frontier model writes adversarial tests, a local worker model implements
against them in a sandbox, and the verified tool joins the registry.

Subsystems: orchestrator, forge, registry, skills, sandbox, evals.
See docs/architecture.md for how they connect.
"""
