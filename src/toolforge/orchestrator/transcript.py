"""Per-run transcript sink — one JSONL file of canonical messages.

Every message the loop appends to history is also serialized here, one
``Message.model_dump_json()`` per line. This is the debugging record (replay
exactly what the model saw) and the substrate the evals subsystem will later
index for the reuse/composition/held-out graphs. SQLite is a future slice; the
JSONL format is deliberately the same canonical ``Message`` schema so a later
importer is a straight read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from toolforge.providers import Message


def new_run_path(runs_dir: Path) -> Path:
    """A fresh UTC-stamped transcript path under *runs_dir* (created if absent)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return runs_dir / f"{stamp}.jsonl"


class Transcript:
    """Append-only JSONL writer for canonical messages."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, msg: Message) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(msg.model_dump_json())
            fh.write("\n")
