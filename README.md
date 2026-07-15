# toolforge

[![CI](https://github.com/ido2103/tool-forge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ido2103/tool-forge/actions/workflows/ci.yml)

A self-expanding agent: when it lacks a tool for a task, it **forges the tool itself** —
spec → adversarial tests → implementation → sandbox verification → registration. The
toolbox grows over time, and a skills library captures multi-step workflows.

A frontier model (Claude) owns all judgment — task execution, wall detection, spec and
adversarial-test authoring, final review. A separate, cheaper worker model owns the
labor — implementing tools against failing tests until green. The worker backend is
configurable: an API model (default — no local hardware needed) or a local Qwen3.6
(35B-A3B or 27B) behind any OpenAI-compatible endpoint. Frontier tokens for decisions,
cheap tokens for sweat.

**Status:** early scaffold — design is settled, implementation starting.
See [docs/architecture.md](docs/architecture.md) for the overview and
[docs/spec.md](docs/spec.md) for the full design document.

## Results

*(Placeholder — the eval graphs are a first-class deliverable of this project.)*

- Tool reuse rate over time
- Composition depth (tools built from tools)
- Success rate on held-out tasks over time

## Development

```bash
uv sync              # set up environment
uv run pytest        # tests
uv run ruff check .  # lint
uv run ruff format --check .  # format check
uv run mypy          # type-check
```

## License

Copyright © 2026 Ido Assaraf. All rights reserved. See [LICENSE](LICENSE).
