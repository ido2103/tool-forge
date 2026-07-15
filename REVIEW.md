# Code Review Guidelines

Review criteria for pull requests to this repository. Used by the automated
Claude review workflow (`.github/workflows/claude-review.yml`) and by human
reviewers.

## What to check, in priority order

1. **Correctness** — logic errors, unhandled edge cases (empty inputs, missing
   files, sandbox failures), error paths that swallow or misreport failures.
   Read the surrounding code before flagging anything — never speculate about
   code you haven't opened.
2. **Documentation contract** — any change under `src/toolforge/<area>/` must
   update `docs/<area>.md` in the same PR (at minimum its `Status:` line).
   Changes to how subsystems connect must also update `docs/architecture.md`.
   `docs/spec.md` must never be edited. This contract exists because the docs
   are a first-class deliverable of this project (see CLAUDE.md).
3. **Granularity principle** — new tools and modules should be composable
   primitives, not task-specific mega-tools (CLAUDE.md contract 2). Flag
   designs that bundle multiple concerns into one interface.
4. **Type safety** — code must pass strict mypy. Flag `Any` leaking through
   public signatures, missing annotations on public functions, and
   `# type: ignore` without a specific error code.
5. **Test coverage** — new behavior needs tests, and adversarial cases matter
   more than happy paths here: this project's premise is that adversarial
   tests are what make generated code trustworthy.
6. **Simplicity** — flag over-engineering: speculative abstractions, defensive
   code for cases that can't occur, dead code, features nobody asked for.

## What not to flag

- Formatting or style that ruff / ruff-format already enforces — CI owns that,
  and duplicate comments are noise.
- Nitpicks that contradict the existing idiom of the surrounding code.
- Missing docstrings or comments, unless the code is genuinely unreadable
  without them.

## How to report

- Verify each finding against the actual code before posting it — open the
  file, confirm the claim holds. Drop any finding you cannot confirm.
- Rate each finding CRITICAL, HIGH, MEDIUM, or LOW, and lead with the most
  severe.
- For each finding give: what is wrong, the concrete failure it causes, and a
  suggested fix.
- If there are no findings, say so in one short comment. Do not invent
  findings to appear thorough.
