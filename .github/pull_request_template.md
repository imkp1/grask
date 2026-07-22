<!--
Explain why, not what. The diff already says what.
-->

## What this changes

## Why

<!--
If you changed a rule that is enforced in code rather than prompted for — the
evidence rule, the one-question rule — say why the code was still the right place
for it, or why it no longer is.
-->

## Checks

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check .` passes
- [ ] `uv run mypy` passes
- [ ] New behaviour has a test that fails without the change
- [ ] No test added here calls a real model (calibration-marked tests excepted)
- [ ] Nothing derived from a real transcript appears in the diff — fixtures, logs, or commit messages
