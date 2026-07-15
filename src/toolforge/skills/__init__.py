"""Skills: markdown workflow playbooks, the second tier of the library.

Tools are code; skills are multi-step sequences with judgment, authored
by the orchestrator after successful multi-step tasks and referencing
tools by name. Each skill carries description frontmatter that the
harness appends to the orchestrator's system prompt; the orchestrator
decides when to load a skill's full body. Every forged tool also gets a
companion usage skill before its first live use.
"""
