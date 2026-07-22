# docs

Design notes only. Two kinds, and they are not equally current.

| File | What it is | Current? |
|---|---|---|
| `design/grill-design.md` | The living overview: how grill works and why each part is shaped that way. | Yes. |
| `design/2026-*.md` | The reasoning behind one change, written before it and dated. | No — a record. |

**The dated notes describe what was decided on that date, not the code today.** They cite
line numbers, and they name modules that have since been merged or deleted — `judge.py`
and `backfill.py` both appear, and neither exists now. That is what makes them worth
keeping: a note rewritten to match the current code stops being evidence of why the code
is that way. It also means you should not grep them for architecture.

For how grill works today, in order: the README, then `design/grill-design.md`, then the
source. `src/grill/` carries the rationale for anything load-bearing in its docstrings,
and those are maintained.

New work adds a dated note here stating what is being decided and what the alternative
was. The existing notes are the format.

The `/grill` skill is not documentation and does not live here — it ships, from
[`skill/SKILL.md`](../skill/SKILL.md).
