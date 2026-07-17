"""System-prompt loading for the orchestrator.

The prompt is a markdown file shipped as package data so the harness stays thin
(criteria live in repo files, not in Python string literals). ``load_system_prompt``
reads the bundled default unless an override path is configured.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def load_system_prompt(override_path: Path | None = None) -> str:
    """Return the system prompt text — from *override_path* if given, else bundled."""
    if override_path is not None:
        return override_path.read_text(encoding="utf-8")
    return (files("toolforge.orchestrator.prompts") / "system.md").read_text(encoding="utf-8")
