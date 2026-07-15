"""Registry: the growing toolbox.

Stores tool specs, implementations, their tests, and usage stats.
Retrieval runs BEFORE the forge fires so the orchestrator can reuse or
compose existing tools instead of forging duplicates. A periodic
curator (v2) merges near-duplicates, deprecates flaky tools, and
promotes battle-tested ones, using registered tests as the regression
suite.
"""
