"""Transcript tests — JSONL round-trip and run-path creation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from toolforge.orchestrator.transcript import Transcript, new_run_path
from toolforge.providers import Message, TextBlock


def _msg(role: Literal["user", "assistant"], text: str) -> Message:
    return Message(
        role=role,
        content=[TextBlock(text=text)],
        ts=datetime.now(tz=UTC),
    )


def test_append_writes_one_json_line_per_message(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "run.jsonl")
    t.append(_msg("user", "hello"))
    t.append(_msg("assistant", "hi there"))

    lines = (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # Each line round-trips back into a canonical Message.
    restored = [Message.model_validate_json(line) for line in lines]
    assert restored[0].role == "user"
    assert restored[0].text == "hello"
    assert restored[1].text == "hi there"


def test_new_run_path_is_utc_stamped_and_creates_dir(tmp_path: Path) -> None:
    runs = tmp_path / "nested" / "runs"
    path = new_run_path(runs)
    assert path.parent == runs
    assert runs.is_dir()
    assert path.name.endswith("Z.jsonl")


def test_new_run_paths_are_unique(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    a = new_run_path(runs)
    b = new_run_path(runs)
    assert a != b  # microsecond precision in the stamp
