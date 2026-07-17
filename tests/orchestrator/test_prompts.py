"""System-prompt loader tests — bundled default and file override."""

from __future__ import annotations

from pathlib import Path

from toolforge.orchestrator.prompts import load_system_prompt


def test_bundled_prompt_loads() -> None:
    text = load_system_prompt()
    assert "Toolforge" in text
    assert "/workspace" in text  # environment section present
    assert "run_bash" in text
    # wall-awareness line (opted-in decision)
    assert "lack a tool" in text.lower() or "missing" in text.lower()


def test_override_path_wins(tmp_path: Path) -> None:
    custom = tmp_path / "sys.md"
    custom.write_text("custom prompt body", encoding="utf-8")
    assert load_system_prompt(custom) == "custom prompt body"
