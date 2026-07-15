"""Orchestrator: the frontier-model brain of the system.

Owns all judgment calls: works tasks ReAct-style with the registered
toolset, detects capability walls (missing tool vs. tool misuse vs.
impossible task), authors tool specs and skills, and runs the final
satisfaction review (holdout tests / spec-conformance) after the forge
reports green.
"""
