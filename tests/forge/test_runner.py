"""Runner contract tests — the exact stdout/exit-code surface a forged tool gets.

The runner is executed the way the container executes it (a real subprocess of
``python3 _runner.py <name> <b64> <tools_root>``), just host-side against a
tmp tools dir — no Docker involved.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import toolforge.forge.runner as runner_module

_RUNNER = Path(runner_module.__file__)


def _write_tool(tools_root: Path, name: str, source: str) -> None:
    tool_dir = tools_root / name
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.py").write_text(source, encoding="utf-8")


def _run(tools_root: Path, name: str, payload: Any) -> tuple[int, str]:
    b64 = (
        payload
        if isinstance(payload, str)
        else base64.b64encode(json.dumps(payload).encode()).decode()
    )
    proc = subprocess.run(
        [sys.executable, str(_RUNNER), name, b64, str(tools_root)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode, proc.stdout


def test_str_return_printed_verbatim(tmp_path: Path) -> None:
    _write_tool(tmp_path, "greet", "def run(name):\n    return f'hello {name}'\n")
    code, out = _run(tmp_path, "greet", {"name": "ido"})
    assert code == 0
    assert out == "hello ido\n"


def test_json_return_serialized(tmp_path: Path) -> None:
    _write_tool(tmp_path, "adder", "def run(a, b):\n    return {'sum': a + b}\n")
    code, out = _run(tmp_path, "adder", {"a": 2, "b": 3})
    assert code == 0
    assert json.loads(out) == {"sum": 5}


def test_none_return_is_null(tmp_path: Path) -> None:
    _write_tool(tmp_path, "noop", "def run():\n    return None\n")
    code, out = _run(tmp_path, "noop", {})
    assert code == 0
    assert out == "null\n"


def test_non_serializable_return_is_tool_error(tmp_path: Path) -> None:
    _write_tool(tmp_path, "weird", "def run():\n    return object()\n")
    code, out = _run(tmp_path, "weird", {})
    assert code == 1
    assert "[tool error: run() returned a non-JSON-serializable object]" in out


def test_raising_tool_reports_traceback(tmp_path: Path) -> None:
    _write_tool(tmp_path, "boom", "def run():\n    raise ValueError('kaput')\n")
    code, out = _run(tmp_path, "boom", {})
    assert code == 1
    assert out.startswith("[tool error]")
    assert "ValueError: kaput" in out


def test_missing_required_kwarg_is_typeerror(tmp_path: Path) -> None:
    _write_tool(tmp_path, "strict", "def run(url):\n    return url\n")
    code, out = _run(tmp_path, "strict", {})
    assert code == 1
    assert "TypeError" in out
    assert "url" in out


def test_missing_tool_is_harness_error(tmp_path: Path) -> None:
    code, out = _run(tmp_path, "ghost", {})
    assert code == 2
    assert "[forged-tool harness error:" in out


def test_import_failure_is_harness_error(tmp_path: Path) -> None:
    _write_tool(tmp_path, "broken", "import nonexistent_module_xyz\n")
    code, out = _run(tmp_path, "broken", {})
    assert code == 2
    assert "importing" in out
    assert "ModuleNotFoundError" in out


def test_no_run_function_is_harness_error(tmp_path: Path) -> None:
    _write_tool(tmp_path, "runless", "def other():\n    return 1\n")
    code, out = _run(tmp_path, "runless", {})
    assert code == 2
    assert "no callable run()" in out


def test_bad_base64_is_harness_error(tmp_path: Path) -> None:
    _write_tool(tmp_path, "greet", "def run():\n    return 'x'\n")
    code, out = _run(tmp_path, "greet", "!!!not-base64!!!")
    assert code == 2
    assert "base64" in out


def test_non_object_json_is_harness_error(tmp_path: Path) -> None:
    _write_tool(tmp_path, "greet", "def run():\n    return 'x'\n")
    b64 = base64.b64encode(b"[1, 2, 3]").decode()
    code, out = _run(tmp_path, "greet", b64)
    assert code == 2
    assert "not a JSON object" in out


def test_bad_argv_is_harness_error(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_RUNNER), "onlyname"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "usage:" in proc.stdout
