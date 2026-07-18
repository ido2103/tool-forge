"""Manifest round-trip and rejection tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from toolforge.forge.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    load_manifest,
    write_manifest,
)


def _manifest(name: str = "fetch_rss") -> Manifest:
    return Manifest(
        name=name,
        description="Fetch an RSS feed URL and return its entries as titled text.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The feed URL."}},
            "required": ["url"],
        },
        behavior="Returns one line per entry; on HTTP failure returns an error string.",
        gap_analysis="run_bash cannot parse RSS without per-call boilerplate scripts",
        holdout_evidence="ran 3 unseen feeds; outputs matched",
        created_at="2026-07-18T00:00:00+00:00",
        allowed_domains=["feeds.example.com"],
        examples=[{"input": {"url": "https://x.com/feed"}, "output": "Title: hi"}],
        test_report="4 passed",
    )


def _tool_dir(tmp_path: Path, name: str = "fetch_rss") -> Path:
    tool_dir = tmp_path / name
    tool_dir.mkdir()
    (tool_dir / "tool.py").write_text("def run(url):\n    return url\n")
    return tool_dir


def test_round_trip(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    write_manifest(_manifest(), tool_dir)
    assert load_manifest(tool_dir) == _manifest()


def test_missing_manifest_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    with pytest.raises(ManifestError, match="missing"):
        load_manifest(tool_dir)


def test_invalid_json_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    (tool_dir / MANIFEST_FILENAME).write_text("{nope")
    with pytest.raises(ManifestError, match="unreadable"):
        load_manifest(tool_dir)


def test_non_object_json_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    (tool_dir / MANIFEST_FILENAME).write_text("[1, 2]")
    with pytest.raises(ManifestError, match="not a JSON object"):
        load_manifest(tool_dir)


def _write_raw(tool_dir: Path, **overrides: Any) -> None:
    raw: dict[str, Any] = json.loads(json.dumps(_manifest(tool_dir.name).__dict__, default=list))
    raw.update(overrides)
    (tool_dir / MANIFEST_FILENAME).write_text(json.dumps(raw))


def test_wrong_schema_version_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    _write_raw(tool_dir, schema_version=99)
    with pytest.raises(ManifestError, match="schema_version"):
        load_manifest(tool_dir)


@pytest.mark.parametrize("bad_name", ["has space", "", None, 42])
def test_invalid_name_rejected(tmp_path: Path, bad_name: Any) -> None:
    tool_dir = _tool_dir(tmp_path)
    _write_raw(tool_dir, name=bad_name)
    with pytest.raises(ManifestError, match="name"):
        load_manifest(tool_dir)


def test_name_directory_mismatch_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path, name="other_name")
    _write_raw(tool_dir, name="fetch_rss")
    with pytest.raises(ManifestError, match="does not match directory"):
        load_manifest(tool_dir)


def test_blank_description_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    _write_raw(tool_dir, description="   ")
    with pytest.raises(ManifestError, match="description"):
        load_manifest(tool_dir)


def test_bad_input_schema_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    _write_raw(tool_dir, input_schema={"type": "array"})
    with pytest.raises(ManifestError, match="input_schema"):
        load_manifest(tool_dir)


def test_missing_tool_py_rejected(tmp_path: Path) -> None:
    tool_dir = _tool_dir(tmp_path)
    write_manifest(_manifest(), tool_dir)
    (tool_dir / "tool.py").unlink()
    with pytest.raises(ManifestError, match="tool.py"):
        load_manifest(tool_dir)


def test_malformed_provenance_normalized(tmp_path: Path) -> None:
    """Provenance fields never make a tool unloadable — they normalize instead."""
    tool_dir = _tool_dir(tmp_path)
    _write_raw(
        tool_dir,
        behavior=42,
        gap_analysis=None,
        holdout_evidence=[],
        created_at={},
        allowed_domains="not-a-list",
        examples=[{"input": {}}, "junk"],
        test_report=7,
    )
    manifest = load_manifest(tool_dir)
    assert manifest.behavior == ""
    assert manifest.gap_analysis == ""
    assert manifest.holdout_evidence == ""
    assert manifest.created_at == ""
    assert manifest.allowed_domains == []
    assert manifest.examples == [{"input": {}}]
    assert manifest.test_report is None
