# Contributing to grill

grill is alpha and small. Issues, bug reports, and pull requests are all welcome.

## Ground rules

Three of these are unusual enough to state up front, because they are the rules a
reasonable contributor would otherwise break by accident.

**The test suite never calls a model.** Every test in `tests/` runs offline, with no
network and no `claude` subprocess, in well under a second. Each pipeline stage is
injected as a plain callable (`triage=`, `complete=`, `store_factory=`), so the whole
thing is drivable by test doubles. A pull request that makes the ordinary suite reach for
a model will be asked to change, however good the test is — that seam is why the suite is
free to run and why CI can be trusted.

Tests that genuinely need a real model go behind the `calibration` marker. They cost
money, they are not deterministic, and they are deselected by default in `pyproject.toml`.

**Nothing derived from a real transcript belongs in this repository.** Not in a test
fixture, not in an issue, not in a commit message. Transcripts contain the developer's
own prompts and source from every project on their machine. Fixtures use invented paths
like `/Users/dev/projects/example` — keep it that way. `.gitignore` has a block of
patterns as a second line of defence; this project has made that mistake once already.

**Instruction is not a control.** Where a rule has to hold, it is enforced in code rather
than asked for in a prompt — the evidence rule and the one-question rule in `probe.py` and
`triage.py` are the two examples. If you find yourself adding "please always…" to a
prompt to fix a bug, that is a signal the check belongs in Python.

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/imkp1/grill && cd grill
uv sync
```

Use `uv sync` for working on grill, and `uv tool install .` for actually using it —
they are different jobs and the README covers the second.

## The three checks

CI runs exactly these, on 3.12 and 3.13. Run them before opening a pull request:

```bash
uv run pytest          # offline, no model calls, sub-second
uv run ruff check .
uv run mypy
```

`mypy` is `strict` against `src/grill`, because the package ships `py.typed` and that is a
promise to anyone importing it. The two relaxations (`disallow_untyped_defs`,
`disallow_incomplete_defs`) exist so injected test doubles are not pinned to production
signatures; both are commented in `pyproject.toml`. Don't add a third without saying why.

## Where things live

| Path | What |
|---|---|
| `src/grill/` | The package. `llm.py` is the only module that knows a subprocess exists; `cli.py` is the only one that owns a terminal. |
| `tests/` | One file per module, offline. |
| `src/grill/SKILL.md` | The `/grill` delivery surface for Claude Code. It lives inside the package because `grill skill --install` reads it from there, and only files under `src/grill/` reach the wheel. |
| `docs/design/` | Why decisions were made. `grill-design.md` is current; the dated notes are records, and `docs/README.md` says so. |

Anything non-trivial starts with a design note in `docs/design/`, dated, stating what is
being decided and what the alternative was. The existing notes are the format.

## Pull requests

- One change per pull request.
- The three checks pass.
- New behaviour comes with a test that would fail without it.
- If you changed a rule that is enforced in code, say in the description why the code was
  the right place for it.
- Commit messages explain why, not what. The diff already says what.

## Reporting bugs

Capture is silent by design — it never writes to your terminal, so a failure looks like
nothing happening. The log is the only evidence:

```bash
cat ~/.claude/grill/grill.log     # or $GRILL_HOME/grill.log
```

Include the tail of that file. **Read it before pasting it** — it can contain material
extracted from your own sessions. Redact freely; a redacted report is worth more than no
report.

## Security and privacy

grill reads every transcript under `~/.claude/projects/` and sends content to a model. If
you have found a way it leaks, over-collects, or writes outside `GRILL_HOME`, see
[SECURITY.md](SECURITY.md) — please don't open a public issue for that class of bug.

## License

By contributing you agree that your contributions are licensed under the MIT License, the
same as the rest of the project.
