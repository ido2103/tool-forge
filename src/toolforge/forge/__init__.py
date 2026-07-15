"""Forge: turns a tool spec into a verified, registered tool.

Internal loop: frontier model writes adversarial tests from the spec
alone (TDD, before any implementation), then the local worker model
(Qwen) implements against them in a harness until green, with a
docs-RAG tool for real API documentation and a bounded iteration
budget that escalates failures back to the orchestrator.
"""
