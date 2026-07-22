# Capture pipeline — SessionEnd hook + storage — Design

**Date:** 2026-07-21
**Status:** ready to plan
**Scope:** capture-and-store only. Nothing asks a human yet.

## Boundary

The hook fires when a session ends, triages that session, and — if it earns a question —
persists a ready-to-ask probe. Asking, answering, judging, and resurfacing are the next
piece, and this design deliberately stops short of them.

What that buys: the four stages that already exist (extract, triage, seed, probe) stop
being things you run by hand against a corpus and start running on every session you
finish. By the time the ask piece exists there is a table of real probes waiting for it.

## Data flow

```
SessionEnd ─stdin─▶ hook.py ─spawn detached─▶ worker
                    (returns fast)            extract → triage → [silent: record, exit]
                                              → seed → probe → store   (errors → log, exit 0)
```

Three units, each testable alone.

## 1. `storage.py` — the `Store`

SQLite at `~/.claude/grill/grill.db`, overridable by env var for tests. Created on first
open; schema applied idempotently.

### `sessions` — one row per triaged session

| column | type | note |
|---|---|---|
| `session_id` | TEXT PK | idempotency key |
| `transcript_path` | TEXT | |
| `cwd` | TEXT NULL | from `Session` |
| `git_branch` | TEXT NULL | from `Session` |
| `verdict` | TEXT | `ask` \| `silent` \| `error` |
| `signal` | TEXT NULL | |
| `topic` | TEXT NULL | |
| `cost_usd` | REAL NULL | triage cost only; stage costs live on their own rows |
| `triaged_at` | TEXT | ISO-8601 |

Recording silence and errors, not just keeps, is the point of this table. Keep-rate and
failure-rate become queries against the db instead of a corpus run someone has to
remember to do. It is also what makes capture idempotent: a `session_id` already present
means skip, whatever its verdict was.

### `seeds` — the stage-2 output

`id` PK, `session_id` FK, `turn`, `signal`, `topic`, `quotes` (json array), `refs` (json
array), `decision`, `hypothesis`, `cost_usd`, `created_at`.

Mirrors `Seed` one-to-one. `quotes` and `refs` are tuples in Python, JSON arrays on disk —
they are read back whole, never queried into, so a json column is honest about how they're
used.

### `probes` — the stage-3 output

`id` PK, `seed_id` FK, `question`, `criteria` (json array), `cost_usd`, `created_at`.

`Probe` carries a `Rubric` whose `topic` and `hypothesis` already live on the seed;
storing only `criteria` here keeps one owner per fact. The rubric is reassembled from
`seeds.topic` + `seeds.hypothesis` + `probes.criteria` when the ask piece needs it.

### Not yet

No `verdicts`, no `resurface_dates`. YAGNI until something asks a human — and both are
additive tables, so adding them later is a migration that touches nothing existing.

## 2. `capture.py` — the orchestration

```
capture_session(transcript_path, store)
```

Top to bottom:

1. `extract(path)` → `Session`. No human turns ⇒ record `silent`, return.
2. `triage(session)` → `TriageVerdict`. `silent` ⇒ record and return.
3. `ask` ⇒ `extract_dialogue(path)` → `seed(dialogue, moment)` → `probe(seed, dialogue)`.
4. Store session + seed + probe.

Errors: the whole body is wrapped. Any `LLMError` or unexpected exception records an
`error` row for the session, appends a traceback to `~/.claude/grill/grill.log`, and
returns. **`capture_session` never raises.** It runs detached with nothing watching its
exit code, so raising would only mean losing the reason.

An already-seen `session_id` returns before spending anything.

## 3. `hook.py` — the entry point

`python -m grill.hook`. Reads the SessionEnd JSON from stdin, pulls `session_id` and
`transcript_path`, spawns the capture worker detached (`setsid`, `stdin=devnull`,
output to the log), exits 0 immediately.

Non-blocking by construction. Whether the harness honours an async hook is not something
this depends on: the parent is gone before the first model call starts. Malformed or
missing stdin exits 0 without spawning — a hook that fails loudly on the way out of a
session is worse than one that silently does nothing.

## Testing (TDD)

- **storage** — temp-db unit tests: round-trip a seed and a probe, foreign keys hold,
  double-capture of the same `session_id` is a no-op.
- **capture** — control flow with injected fake triage/seed/probe, no model spend:
  silent stores exactly one session row and no seed; ask stores session + seed + probe;
  a seed `LLMError` is swallowed, recorded as `error`, and not raised.
- **hook** — stdin parse, spawn called with the right argv, returns fast; spawn
  monkeypatched.
- **one real end-to-end smoke** against a known-keep transcript into a temp db.
  Marked `calibration` — it costs roughly $0.59, which is the measurement, not a
  side effect.

## Wiring

- `grill-hook` console entry point in `pyproject.toml`.
- Install editable into the venv.
- Global `SessionEnd` hook in `~/.claude/settings.json` pointing at the venv's absolute
  `grill-hook` path, so it works from any cwd.

## Open

Stage 1 topic instability is known and unfixed — triage keeps consistently but names the
topic arbitrarily. Capture stores whatever topic triage gives it. That surfaces the
instability in the db rather than hiding it, which is the right place for it to be visible
when the ask piece starts deduping.
