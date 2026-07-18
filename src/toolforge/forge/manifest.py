"""The forged-tool manifest — the on-disk record that makes a tool loadable.

The filesystem is the registry's durable store: each promoted tool is a
directory ``<tools_path>/<name>/`` holding ``tool.py``, ``manifest.json``, and
(when the candidate carried tests) ``test_tool.py``. The manifest is everything
the boot loader needs to rebuild the ``RegisteredTool`` (name, description,
input_schema) plus provenance the curator and evals will want later (behavior
contract, gap analysis, holdout evidence, timestamps). No database — the
directory IS the record, so code and metadata cannot drift apart.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ManifestError(Exception):
    """A manifest that cannot be trusted to describe a loadable tool."""


@dataclass
class Manifest:
    """The persisted spec + provenance of one forged tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    behavior: str
    gap_analysis: str
    holdout_evidence: str
    created_at: str
    allowed_domains: list[str] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)
    test_report: str | None = None
    schema_version: int = MANIFEST_SCHEMA_VERSION


def validate_input_schema(schema: Any) -> str | None:
    """Structural sanity of an orchestrator-authored tool schema; reason or ``None``.

    Deliberately hand-rolled: full JSON-Schema validation would need a new
    dependency, and the API rejects deeper invalidity at registration time anyway.
    """
    if not isinstance(schema, dict):
        return "it is not an object"
    if schema.get("type") != "object":
        return "its 'type' must be \"object\""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return "it must have a 'properties' object"
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            return f"property {prop_name!r} must be a schema object"
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            return "'required' must be a list of property names"
        for entry in required:
            if entry not in properties:
                return f"'required' names undefined property {entry!r}"
    return None


def write_manifest(manifest: Manifest, tool_dir: Path) -> None:
    """Write ``manifest.json`` into *tool_dir* (which must already exist)."""
    payload = json.dumps(asdict(manifest), indent=2, ensure_ascii=False)
    (tool_dir / MANIFEST_FILENAME).write_text(payload + "\n", encoding="utf-8")


def load_manifest(tool_dir: Path) -> Manifest:
    """Load and validate the manifest of one tool directory.

    Raises :class:`ManifestError` for anything that would make the tool
    unloadable or ambiguous: unreadable/invalid JSON, wrong schema_version, a
    name that is invalid or does not match the directory, a blank description,
    a bad input_schema, or a missing ``tool.py``. Provenance fields are
    normalized but never rejected.
    """
    path = tool_dir / MANIFEST_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"missing {MANIFEST_FILENAME}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"unreadable {MANIFEST_FILENAME}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{MANIFEST_FILENAME} is not a JSON object")

    version = raw.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"unsupported schema_version {version!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ManifestError(f"invalid tool name {name!r}")
    if name != tool_dir.name:
        raise ManifestError(f"name {name!r} does not match directory {tool_dir.name!r}")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ManifestError("'description' must be a non-empty string")
    input_schema = raw.get("input_schema")
    schema_problem = validate_input_schema(input_schema)
    if schema_problem is not None:
        raise ManifestError(f"invalid input_schema: {schema_problem}")
    assert isinstance(input_schema, dict)  # validate_input_schema guarantees it
    if not (tool_dir / "tool.py").is_file():
        raise ManifestError("tool.py is missing")

    def _text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    domains = raw.get("allowed_domains")
    examples = raw.get("examples")
    test_report = raw.get("test_report")
    return Manifest(
        name=name,
        description=description,
        input_schema=input_schema,
        behavior=_text("behavior"),
        gap_analysis=_text("gap_analysis"),
        holdout_evidence=_text("holdout_evidence"),
        created_at=_text("created_at"),
        allowed_domains=[d for d in domains if isinstance(d, str)]
        if isinstance(domains, list)
        else [],
        examples=[e for e in examples if isinstance(e, dict)] if isinstance(examples, list) else [],
        test_report=test_report if isinstance(test_report, str) else None,
    )
