# toolforge

A self-expanding agent: when it lacks a tool for a task, it **forges the tool itself** —
spec → adversarial tests → implementation → sandbox verification → registration. The
toolbox grows over time, and a skills library captures multi-step workflows.

A frontier model (Claude) owns all judgment — task execution, wall detection, spec and
adversarial-test authoring, final review. A local model (Qwen3.6-35B-A3B) owns the
labor — implementing tools against failing tests until green. Frontier tokens for
decisions, local tokens for sweat.

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
uv run mypy          # type-check
```

## License

Copyright © 2026 Ido Assaraf. All rights reserved. See [LICENSE](LICENSE).
