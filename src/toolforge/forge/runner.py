"""The forged-tool runner — harness-owned JSON plumbing around ``run(**input)``.

This file is copied verbatim to ``<tools_path>/_runner.py`` at REPL boot (see
``runtime.install_runner``) and executed INSIDE the sandbox container as
``python3 /tools/_runner.py <name> <b64-json> [tools_root]``. It must therefore
stay stdlib-only and import nothing from toolforge. Keeping the I/O boilerplate
here means generated ``tool.py`` files are plain ``def run(...)`` functions —
less surface for the forge worker to get wrong, and pytest can import them
directly.

Exit codes are the machine half of the result contract (the human half is the
bracketed message on stdout):

- 0 — ``run()`` returned; stdout is the result (str verbatim, else JSON).
- 1 — the TOOL failed: ``run()`` raised, or returned a non-JSON-serializable
  value. stdout starts with ``[tool error...]``.
- 2 — the HARNESS failed before/around the tool: bad argv, undecodable input,
  ``tool.py`` missing or unimportable, no callable ``run``. stdout starts with
  ``[forged-tool harness error: ...]``.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import traceback

_DEFAULT_TOOLS_ROOT = "/tools"


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("[forged-tool harness error: usage: _runner.py <name> <b64-json> [tools_root]]")
        return 2
    name = argv[1]
    tools_root = argv[3] if len(argv) == 4 else _DEFAULT_TOOLS_ROOT

    try:
        decoded = json.loads(base64.b64decode(argv[2], validate=True))
    except Exception:
        print("[forged-tool harness error: input was not base64-encoded JSON]")
        return 2
    if not isinstance(decoded, dict):
        print("[forged-tool harness error: decoded input is not a JSON object]")
        return 2

    path = f"{tools_root}/{name}/tool.py"
    spec = importlib.util.spec_from_file_location(f"forged_{name}", path)
    if spec is None or spec.loader is None:
        print(f"[forged-tool harness error: cannot load {path}]")
        return 2
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except FileNotFoundError:
        print(f"[forged-tool harness error: {path} does not exist]")
        return 2
    except Exception:
        print(f"[forged-tool harness error: importing {path} failed]\n" + traceback.format_exc())
        return 2
    run = getattr(module, "run", None)
    if not callable(run):
        print(f"[forged-tool harness error: {path} defines no callable run()]")
        return 2

    try:
        result = run(**decoded)
    except Exception:
        print("[tool error]\n" + traceback.format_exc())
        return 1

    if isinstance(result, str):
        print(result)
        return 0
    try:
        print(json.dumps(result))
    except (TypeError, ValueError):
        print(f"[tool error: run() returned a non-JSON-serializable {type(result).__name__}]")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main(sys.argv))
