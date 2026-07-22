# Claude Slash-Command Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve grill probes inside Claude Code via a `/grill` skill, backed by two new non-interactive subcommands: `grill serve --json` and `grill record <probe_id>`.

**Architecture:** `cli.py` gains `serve` and `record` subcommands; the pure grading function moves into `ask.py` beside `ask()` so there is exactly one place that grades; `storage.py` gains an option-count cap on `next_probe` and a by-id lookup. The interactive `grill` command and `ask()` behavior are unchanged. A user-level skill file (`~/.claude/skills/grill/SKILL.md`) tells Claude how to drive the two subcommands with its native question UI.

**Tech Stack:** Python 3.12+, stdlib only (argparse, json, sqlite3), pytest via `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-07-22-claude-slash-command-delivery-design.md`

## Global Constraints

- Repo root: `~/projects/grill`. All commands run from there.
- Run tests with `uv run pytest <path> -v`. Never invoke bare `uv run grill` (interactive; it blocks). `grill serve --json` and `grill record` are non-interactive and safe to run — but only against a test `GRILL_HOME`, never the real database.
- Zero model calls anywhere in this plan. Everything is scripted-console / real-SQLite testing.
- Blind serve: `serve` output must never include `correct_idx` or `explanation` — assert on raw stdout text, not just parsed keys.
- Claude's question UI cap: 4 options. Generation emits 3–4. `LETTERS = "abcde"` and the terminal path stay unchanged.
- Commit as the repo's existing git config. No `Co-Authored-By` trailer, ever.
- Match the codebase's comment style: comments explain constraints and design pressure, not what the next line does. Do not delete existing comments you aren't invalidating.

---

### Task 1: Generation cap 3–4 (`probe.py`)

**Files:**
- Modify: `src/grill/probe.py` (line 28 `MAX_OPTIONS`, line 78 `CORRECTION` text)
- Test: `tests/test_probe.py` (lines 4, 94–99)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `MAX_OPTIONS == 4`. `validate_choices` (shared with `backfill.py`, which imports `MAX_OPTIONS`) now rejects 5 options — the backfill picks the change up for free.

- [ ] **Step 1: Update the failing test**

In `tests/test_probe.py`, replace the `test_rejects_more_than_five_options` method (lines 94–99) with:

```python
    def test_rejects_more_than_four_options(self):
        """Claude's question UI takes at most 4 options, so 5 is unservable there."""
        with pytest.raises(LLMError):
            parse_probe(
                SEED,
                completion(response(options=[f"option {n}" for n in range(5)], correct=0)),
            )
```

Also update the module docstring on line 4: change `3-5 pairwise-distinct options` to `3-4 pairwise-distinct options`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_probe.py::TestStructuralGates::test_rejects_more_than_four_options -v`
(If the class name differs, run `uv run pytest tests/test_probe.py -k rejects_more_than_four -v`.)
Expected: FAIL — 5 options currently pass the gate, so `pytest.raises` gets no exception.

- [ ] **Step 3: Change the gate and the correction text**

In `src/grill/probe.py`:

```python
MAX_OPTIONS = 4
```

(was `5`; add a trailing comment: `# Claude's native question UI takes at most 4 options.`)

In the `CORRECTION` template, change the rule sentence `3 to 5 one-line options` to `3 to 4 one-line options`. The main `PROMPT` already interpolates `{min_options}`/`{max_options}`, so it updates itself.

- [ ] **Step 4: Run the full probe + backfill suites**

Run: `uv run pytest tests/test_probe.py tests/test_backfill.py -v`
Expected: all PASS (backfill shares `validate_choices` and `MAX_OPTIONS`; no test there mints 5 options).

- [ ] **Step 5: Commit**

```bash
git add src/grill/probe.py tests/test_probe.py
git commit -m "feat: cap generated probes at 4 options for the Claude question UI"
```

---

### Task 2: Pure grading function in `ask.py`

**Files:**
- Modify: `src/grill/ask.py` (add `resolution` and `grade` after `_unservable`, refactor `ask`)
- Test: `tests/test_ask.py` (new `TestGrade` class)

**Interfaces:**
- Consumes: existing `PendingProbe`, `Interrogation`, `AnswerTurn`, `LETTERS`, `CONFIDENCES`, outcome constants — all already in `ask.py`.
- Produces (Tasks 4–5 import these from `grill.ask`):
  - `resolution(pending: PendingProbe, outcome: str, *, confidence: int | None = None, objection: str | None = None, turns: tuple[AnswerTurn, ...] = (), reasoning: str = "") -> Interrogation`
  - `grade(pending: PendingProbe, confidence: int, pick: str) -> Interrogation` — raises `ValueError` on a confidence outside `CONFIDENCES` or a pick letter that names no stored option.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ask.py` (add `grade` to the existing `from grill.ask import (...)`):

```python
class TestGrade:
    """The pure (pending, confidence, pick) -> Interrogation map the record path uses."""

    def test_the_correct_pick_passes(self):
        result = grade(PENDING, 95, "b")

        assert result.outcome == PASSED
        assert result.confidence == 95
        assert result.probe_id == PENDING.probe_id
        assert result.turns == (
            AnswerTurn(turn=0, question=PENDING.question, answer=PENDING.options[1]),
        )
        assert result.reasoning == PENDING.explanation
        assert result.cost_usd == 0.0

    def test_a_wrong_pick_fails(self):
        result = grade(PENDING, 40, "a")

        assert result.outcome == FAILED
        assert result.turns[0].answer == PENDING.options[0]

    def test_the_pick_is_case_insensitive(self):
        assert grade(PENDING, 70, "B").outcome == PASSED

    def test_a_letter_beyond_the_stored_options_is_rejected(self):
        with pytest.raises(ValueError):
            grade(PENDING, 95, "d")  # PENDING has three options: a-c

    def test_a_multi_character_pick_is_rejected(self):
        with pytest.raises(ValueError):
            grade(PENDING, 95, "ab")

    def test_an_unknown_confidence_is_rejected(self):
        with pytest.raises(ValueError):
            grade(PENDING, 100, "b")
```

`AnswerTurn` also needs importing in the test module's import block.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_ask.py::TestGrade -v`
Expected: FAIL with `ImportError: cannot import name 'grade'`.

- [ ] **Step 3: Implement `resolution` and `grade`**

In `src/grill/ask.py`, after `_unservable` and before `ask`:

```python
def resolution(
    pending: PendingProbe,
    outcome: str,
    *,
    confidence: int | None = None,
    objection: str | None = None,
    turns: tuple[AnswerTurn, ...] = (),
    reasoning: str = "",
) -> Interrogation:
    """An Interrogation for `pending`, however it ended.

    Shared by the interactive loop and the non-interactive record path so both
    write identically shaped rows. `cost_usd` is always 0.0: there is no model
    call anywhere in an ask, whichever surface drove it.
    """
    return Interrogation(
        probe_id=pending.probe_id,
        outcome=outcome,
        confidence=confidence,
        objection=objection,
        turns=turns,
        cost_usd=0.0,
        reasoning=reasoning,
    )


def grade(pending: PendingProbe, confidence: int, pick: str) -> Interrogation:
    """(pending, confidence, pick letter) -> a mechanically graded Interrogation.

    The one place a pick becomes a verdict; `ask` and `grill record` both land
    here. Raises ValueError rather than guessing on bad input — the caller
    decides how to surface it. Never call this on a row `_unservable` flags.
    """
    if confidence not in CONFIDENCES:
        raise ValueError(f"confidence must be one of {', '.join(map(str, CONFIDENCES))}")
    valid = LETTERS[: len(pending.options)]
    letter = pick.strip().lower()
    if len(letter) != 1 or letter not in valid:
        raise ValueError(f"pick must be a single letter, a-{valid[-1]}")
    picked = valid.index(letter)
    return resolution(
        pending,
        PASSED if picked == pending.correct_idx else FAILED,
        confidence=confidence,
        turns=(AnswerTurn(turn=0, question=pending.question, answer=pending.options[picked]),),
        reasoning=pending.explanation,
    )
```

- [ ] **Step 4: Refactor `ask` to delegate**

Inside `ask()`:

1. Replace the body of the inner `done` closure with a delegation:

```python
    def done(
        outcome: str,
        *,
        confidence: int | None = None,
        objection: str | None = None,
        turns: tuple[AnswerTurn, ...] = (),
        reasoning: str = "",
    ) -> Interrogation:
        return resolution(
            pending,
            outcome,
            confidence=confidence,
            objection=objection,
            turns=turns,
            reasoning=reasoning,
        )
```

2. Replace the final grading block (the `picked = valid.index(typed)` / `break` loop tail through the closing `return done(...)`) so the pick loop `break`s with `typed` in hand and the tail reads:

```python
    graded = grade(pending, confidence, typed)
    console.show(f"{'✓' if graded.outcome == PASSED else '✗'} {pending.explanation}")
    return graded
```

Delete the now-dead `picked = valid.index(typed)` line and the `passed = picked == pending.correct_idx` line. The `if len(typed) == 1 and typed in valid:` check in the loop stays — it is what makes the re-prompt hint work; `grade` will simply never see bad input from this path.

- [ ] **Step 5: Run the full ask + cli suites**

Run: `uv run pytest tests/test_ask.py tests/test_cli.py -v`
Expected: all PASS — every pre-existing `ask()` behavior (verdicts, skip, wrong, hints, malformed) is pinned by existing tests and must not change.

- [ ] **Step 6: Commit**

```bash
git add src/grill/ask.py tests/test_ask.py
git commit -m "feat: extract pure resolution/grade functions from the interactive ask"
```

---

### Task 3: Storage — option cap on `next_probe`, `probe_by_id`

**Files:**
- Modify: `src/grill/storage.py` (`next_probe` at line 217, new `_pending_from_row` module function, new `probe_by_id` method)
- Test: `tests/test_storage.py` (extend the existing `next_probe` test class; new `TestProbeById`)

**Interfaces:**
- Consumes: nothing new.
- Produces (Task 4–5 call these on `Store`):
  - `next_probe(*, max_options: int | None = None) -> PendingProbe | None` — with a cap, rows whose options are a valid JSON array longer than the cap are left pending and skipped; rows whose options are *invalid* JSON still come back (they must be served so the caller can record the `error` they are). `max_options=None` behaves exactly as today.
  - `probe_by_id(probe_id: int) -> PendingProbe | None` — the stored probe regardless of TTL or asked-state; `None` for an unknown id or a legacy `options IS NULL` row.

- [ ] **Step 1: Write the failing tests**

In `tests/test_storage.py`, the existing `next_probe` tests live in a class with a `stored` helper (around line 308). Add these tests to that class (adapt the helper calls to its exact signature — it takes `session_id`, `created_at`, `topic` and returns the probe id):

```python
    def test_a_cap_skips_a_row_with_more_options_and_leaves_it_pending(self, store: Store):
        probe_id = self.stored(
            store, session_id="s1", created_at=iso_days_ago(0), topic="wide"
        )
        store.conn.execute(
            "UPDATE probes SET options = ? WHERE id = ?",
            (json.dumps([f"option {n}" for n in range(5)]), probe_id),
        )
        store.conn.commit()

        assert store.next_probe(max_options=4) is None
        # The terminal path (no cap) can still serve it: skipped, not consumed.
        uncapped = store.next_probe()
        assert uncapped is not None and uncapped.probe_id == probe_id

    def test_a_cap_still_serves_a_row_whose_options_are_not_json(self, store: Store):
        """Invalid JSON must reach the caller so it can be recorded as an error."""
        probe_id = self.stored(
            store, session_id="s2", created_at=iso_days_ago(0), topic="broken"
        )
        store.conn.execute(
            "UPDATE probes SET options = ? WHERE id = ?", ("not json", probe_id)
        )
        store.conn.commit()

        pending = store.next_probe(max_options=4)

        assert pending is not None
        assert pending.probe_id == probe_id
        assert pending.options == ()

    def test_no_cap_is_the_default_and_unchanged(self, store: Store):
        probe_id = self.stored(
            store, session_id="s3", created_at=iso_days_ago(0), topic="plain"
        )

        pending = store.next_probe()

        assert pending is not None and pending.probe_id == probe_id
```

And a new class at the bottom of the file:

```python
class TestProbeById:
    def test_round_trips_the_stored_probe(self, store: Store):
        store.record_session(
            session_id="0198e4f1",
            transcript_path="/tmp/t.jsonl",
            cwd=None,
            git_branch=None,
            verdict="kept",
        )
        seed_id = store.add_seed(a_seed())
        probe_id = store.add_probe(seed_id, a_probe())

        pending = store.probe_by_id(probe_id)

        assert pending is not None
        assert pending.probe_id == probe_id
        assert pending.question == a_probe().question
        assert pending.options == a_probe().options
        assert pending.correct_idx == a_probe().correct_idx
        assert pending.rubric.topic == a_probe().rubric.topic

    def test_an_unknown_id_is_none(self, store: Store):
        assert store.probe_by_id(999) is None
```

Add `import json` to the test module's imports if not already present.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_storage.py -k "cap or ProbeById or no_cap" -v`
Expected: FAIL — `next_probe() got an unexpected keyword argument 'max_options'` and `AttributeError: ... probe_by_id`.

- [ ] **Step 3: Implement**

In `src/grill/storage.py`:

1. Add a module-level function above `class Store` (move the parsing block out of `next_probe` verbatim, including its comment):

```python
def _pending_from_row(row: sqlite3.Row) -> PendingProbe:
    """One probes-join-seeds row, parsed defensively rather than trusted.

    A row that fails to parse is returned anyway and becomes ask's `error`
    outcome, which is the one error the design keeps.
    """
    try:
        loaded = json.loads(row["options"])
        options = (
            tuple(o for o in loaded if isinstance(o, str))
            if isinstance(loaded, list)
            else ()
        )
    except (TypeError, ValueError):
        options = ()

    correct_idx = row["correct_idx"]
    return PendingProbe(
        probe_id=int(row["id"]),
        question=row["question"],
        options=options,
        correct_idx=int(correct_idx) if isinstance(correct_idx, int) else None,
        explanation=row["explanation"] or "",
        rubric=Rubric(topic=row["topic"], hypothesis=row["hypothesis"]),
        created_at=row["created_at"],
    )
```

2. Rewrite `next_probe` to take the cap and use named SQL parameters (keep the existing docstring and inline comments, appending the paragraph below to the docstring):

```python
    def next_probe(self, *, max_options: int | None = None) -> PendingProbe | None:
        # ...existing docstring, plus:
        # `max_options` caps how many options a servable row may carry, for
        # delivery surfaces with a hard UI limit. Over-cap rows are skipped and
        # left pending — another surface may still serve them — while rows whose
        # options are not valid JSON pass the filter deliberately: they must be
        # served so the caller can record the `error` they are.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PROBE_TTL_DAYS)).isoformat()
        row = self.conn.execute(
            "SELECT p.id, p.question, p.options, p.correct_idx, p.explanation,"
            " p.created_at, s.topic, s.hypothesis"
            " FROM probes p"
            " JOIN seeds s ON s.id = p.seed_id"
            " LEFT JOIN asks a ON a.probe_id = p.id"
            " WHERE a.id IS NULL AND p.created_at >= :cutoff"
            " AND p.options IS NOT NULL"
            " AND (:cap IS NULL OR json_valid(p.options) = 0"
            "      OR json_array_length(p.options) <= :cap)"
            " ORDER BY p.created_at DESC, p.id DESC"
            " LIMIT 1",
            {"cutoff": cutoff, "cap": max_options},
        ).fetchone()

        return None if row is None else _pending_from_row(row)
```

(`json_valid` guards `json_array_length`, which raises on malformed JSON and would otherwise kill the whole query.)

3. Add `probe_by_id` after `next_probe`:

```python
    def probe_by_id(self, probe_id: int) -> PendingProbe | None:
        """The stored probe, whether or not it is still pending.

        No TTL or asked filter: the record path targets a probe `serve` already
        named, and a double record is refused by UNIQUE(probe_id) at write time,
        not by this read. Legacy free-text rows (`options IS NULL`) stay
        invisible here for the same reason `next_probe` never serves them.
        """
        row = self.conn.execute(
            "SELECT p.id, p.question, p.options, p.correct_idx, p.explanation,"
            " p.created_at, s.topic, s.hypothesis"
            " FROM probes p"
            " JOIN seeds s ON s.id = p.seed_id"
            " WHERE p.id = ? AND p.options IS NOT NULL",
            (probe_id,),
        ).fetchone()

        return None if row is None else _pending_from_row(row)
```

- [ ] **Step 4: Run the storage suite**

Run: `uv run pytest tests/test_storage.py -v`
Expected: all PASS, including every pre-existing `next_probe` test (no-arg behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/grill/storage.py tests/test_storage.py
git commit -m "feat: option-count cap on next_probe and a by-id probe lookup"
```

---

### Task 4: `grill serve --json` (`cli.py`)

**Files:**
- Modify: `src/grill/cli.py`
- Create: `tests/test_serve_record.py`

**Interfaces:**
- Consumes: `Store.next_probe(max_options=4)`, `Store.probe_by_id`, `Store.record_ask` (Task 3); `resolution`, `_unservable`, `ERROR` from `grill.ask` (Task 2).
- Produces: `grill serve --json` prints exactly one JSON object on stdout — either `{"probe_id": int, "question": str, "options": [str, ...], "topic": str, "created_at": str}` or `{"pending": null}` — and exits 0. Task 6's skill file drives it. Bare `grill` (interactive) is untouched: `main([])` still runs the interactive path.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve_record.py`:

```python
"""Tests for the non-interactive delivery seam: `grill serve` and `grill record`.

Real Store on a tmp database — the value under test is the wiring from argv to
SQLite and the exact JSON shapes Claude will parse. Blind serve is asserted on
raw stdout, not parsed keys: the answer key must not appear in any encoding.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from grill.cli import main
from grill.probe import Probe, Rubric
from grill.seed import Seed
from grill.storage import Store

RUBRIC = Rubric(
    topic="idempotency of the retry path",
    hypothesis="the developer accepted the key without knowing what it dedupes against",
)


def a_probe() -> Probe:
    return Probe(
        question="What would happen if two retries carried the same idempotency key?",
        options=(
            "The second call is deduplicated to a no-op",
            "The second call fails with a conflict error",
            "Both calls execute and the ledger reconciles later",
        ),
        correct_idx=0,
        explanation="Within the dedupe window the provider replays the first response.",
        rubric=RUBRIC,
        cost_usd=0.13,
    )


def a_seed(session_id: str) -> Seed:
    return Seed(
        session_id=session_id,
        turn=4,
        signal="asked_why",
        topic=RUBRIC.topic,
        quotes=("why do we need an idempotency key here?",),
        refs=("src/api/retry.py",),
        decision="added an idempotency key to the retry wrapper",
        hypothesis=RUBRIC.hypothesis,
        cost_usd=0.21,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "grill.db"


def stored_probe(db: Path, probe: Probe | None = None, session_id: str = "0198e4f1") -> int:
    with Store(db) as store:
        store.record_session(
            session_id=session_id,
            transcript_path="/tmp/t.jsonl",
            cwd=None,
            git_branch=None,
            verdict="kept",
        )
        seed_id = store.add_seed(a_seed(session_id))
        return store.add_probe(seed_id, probe or a_probe())


def run(db: Path, argv: list[str]) -> int:
    return main(argv, store_factory=lambda: Store(db))


def asks_rows(db: Path) -> list:
    with Store(db) as store:
        return store.conn.execute(
            "SELECT probe_id, outcome, confidence, objection FROM asks ORDER BY id"
        ).fetchall()


class TestServe:
    def test_emits_the_probe_without_the_answer_key(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["serve", "--json"])
        out = capsys.readouterr().out

        assert code == 0
        # Blind serve: the raw text carries neither the key nor the explanation.
        assert "correct" not in out
        assert a_probe().explanation not in out
        assert json.loads(out) == {
            "probe_id": probe_id,
            "question": a_probe().question,
            "options": list(a_probe().options),
            "topic": RUBRIC.topic,
            "created_at": json.loads(out)["created_at"],
        }

    def test_consumes_nothing(self, db: Path, capsys):
        probe_id = stored_probe(db)

        run(db, ["serve", "--json"])
        first = json.loads(capsys.readouterr().out)
        run(db, ["serve", "--json"])
        second = json.loads(capsys.readouterr().out)

        assert first["probe_id"] == second["probe_id"] == probe_id
        assert asks_rows(db) == []

    def test_an_empty_queue_is_pending_null(self, db: Path, capsys):
        Store(db).close()  # create the schema, store nothing

        code = run(db, ["serve", "--json"])

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {"pending": None}

    def test_a_five_option_row_is_left_pending_not_consumed(self, db: Path, capsys):
        wide = replace(
            a_probe(), options=tuple(f"option {n}" for n in range(5)), correct_idx=0
        )
        probe_id = stored_probe(db, probe=wide)

        code = run(db, ["serve", "--json"])

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {"pending": None}
        assert asks_rows(db) == []
        # The terminal path can still see it.
        with Store(db) as store:
            pending = store.next_probe()
        assert pending is not None and pending.probe_id == probe_id

    def test_a_malformed_row_records_an_error_and_the_next_row_is_served(
        self, db: Path, capsys
    ):
        broken_id = stored_probe(db, session_id="s-broken")
        with Store(db) as store:
            store.conn.execute(
                "UPDATE probes SET correct_idx = NULL WHERE id = ?", (broken_id,)
            )
            store.conn.commit()
        good_id = stored_probe(db, session_id="s-good")
        # Make the broken row the newest so serve meets it first.
        with Store(db) as store:
            store.conn.execute(
                "UPDATE probes SET created_at = ? WHERE id = ?",
                ("2099-01-01T00:00:00+00:00", broken_id),
            )
            store.conn.commit()

        code = run(db, ["serve", "--json"])
        out = json.loads(capsys.readouterr().out)

        assert code == 0
        assert out["probe_id"] == good_id
        rows = asks_rows(db)
        assert len(rows) == 1
        assert rows[0]["probe_id"] == broken_id
        assert rows[0]["outcome"] == "error"
```

(The `created_at` future-dating trick fails the TTL's *upper* bound never — `next_probe` only filters `>= cutoff` — so a 2099 date simply sorts first. If the suite shows otherwise, use a timestamp a minute in the future instead.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_serve_record.py::TestServe -v`
Expected: FAIL — argparse errors: `error: unrecognized arguments: serve --json` (exit 2 raises `SystemExit`).

- [ ] **Step 3: Implement `serve` in `cli.py`**

Rework `src/grill/cli.py`:

1. Extend the imports and add the UI cap constant:

```python
import argparse
import json

from grill.ask import ERROR, Console, _unservable, ask as _ask, resolution
from grill.storage import Store

NOTHING_PENDING = "nothing to ask about."

# Claude's native question UI takes at most 4 options; rows over the cap are
# left pending for the terminal path rather than consumed.
MAX_UI_OPTIONS = 4
```

2. Add the serve implementation above `main`:

```python
def _serve(store_factory) -> int:
    """Print the next servable probe as JSON, blind: no key, no explanation.

    Consumes nothing — an abandoned Claude session leaves the probe pending,
    matching Ctrl-C in the terminal path. The one write is the same one `ask`
    keeps: a row too broken to grade is recorded as an error so it stops
    blocking the queue, and the loop moves to the next row.
    """
    with store_factory() as store:
        while True:
            pending = store.next_probe(max_options=MAX_UI_OPTIONS)
            if pending is None:
                print(json.dumps({"pending": None}))
                return 0
            if _unservable(pending):
                store.record_ask(resolution(pending, ERROR))
                continue
            print(
                json.dumps(
                    {
                        "probe_id": pending.probe_id,
                        "question": pending.question,
                        "options": list(pending.options),
                        "topic": pending.rubric.topic,
                        "created_at": pending.created_at,
                    }
                )
            )
            return 0
```

3. In `main`, add subparsers before `parse_args` and dispatch after it. The interactive path (everything from `with store_factory() as store:` down) stays exactly as it is, reached when no subcommand was given:

```python
    parser = argparse.ArgumentParser(
        prog="grill", description="Answer one question about something you shipped."
    )
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser(
        "serve", help="print the next pending probe as one JSON object"
    )
    serve_parser.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="emit JSON (the only mode; the flag keeps the contract explicit)",
    )

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(store_factory)
```

4. Update the module docstring's last paragraph: this file now also owns the non-interactive delivery seam its docstring reserved — add one sentence saying `serve`/`record` are that seam, driven by the `/grill` skill.

- [ ] **Step 4: Run serve tests and the old cli tests**

Run: `uv run pytest tests/test_serve_record.py::TestServe tests/test_cli.py -v`
Expected: all PASS — `main([])` must still run the interactive path (the existing `test_cli.py` tests pin that).

- [ ] **Step 5: Commit**

```bash
git add src/grill/cli.py tests/test_serve_record.py
git commit -m "feat: grill serve --json, the blind non-interactive delivery seam"
```

---

### Task 5: `grill record <probe_id>` (`cli.py`)

**Files:**
- Modify: `src/grill/cli.py`
- Test: `tests/test_serve_record.py` (new `TestRecord` class)

**Interfaces:**
- Consumes: `grade`, `resolution`, `_unservable`, `SKIPPED`, `PREMISE_REJECTED`, `PASSED`, `FAILED`, `CONFIDENCES` from `grill.ask`; `Store.probe_by_id`, `Store.record_ask`.
- Produces: `grill record <id> --confidence {95,70,40} --pick {a,b,c,d}` → `{"outcome": "passed"|"failed", "explanation": "..."}`, exit 0. `--skip [--confidence N]` → `{"outcome": "skipped"}`. `--wrong [--confidence N] [--objection TEXT]` → `{"outcome": "premise_rejected"}`. Domain errors → `{"error": "..."}` on stdout, exit 1, no partial writes. Flag-combination misuse → argparse `parser.error` (stderr, exit 2).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_serve_record.py`:

```python
class TestRecord:
    def test_the_correct_pick_passes_with_the_explanation(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--confidence", "95", "--pick", "a"])

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {
            "outcome": "passed",
            "explanation": a_probe().explanation,
        }
        rows = asks_rows(db)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "passed"
        assert rows[0]["confidence"] == 95

    def test_a_wrong_pick_fails_and_stores_the_answer_text(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--confidence", "70", "--pick", "b"])

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {
            "outcome": "failed",
            "explanation": a_probe().explanation,
        }
        with Store(db) as store:
            answers = store.conn.execute("SELECT answer FROM answers").fetchall()
        assert [row["answer"] for row in answers] == [a_probe().options[1]]

    def test_skip_records_skipped(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--skip"])

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {"outcome": "skipped"}
        assert asks_rows(db)[0]["confidence"] is None

    def test_skip_after_the_commit_keeps_the_confidence(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--skip", "--confidence", "70"])

        assert code == 0
        assert asks_rows(db)[0]["confidence"] == 70

    def test_wrong_records_premise_rejected_with_the_objection(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(
            db,
            ["record", str(probe_id), "--wrong", "--objection", "that was the agent"],
        )

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {"outcome": "premise_rejected"}
        assert asks_rows(db)[0]["objection"] == "that was the agent"

    def test_wrong_without_an_objection(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--wrong"])

        assert code == 0
        assert asks_rows(db)[0]["objection"] is None

    def test_a_double_record_is_rejected_without_a_second_row(self, db: Path, capsys):
        probe_id = stored_probe(db)
        run(db, ["record", str(probe_id), "--confidence", "95", "--pick", "a"])
        capsys.readouterr()

        code = run(db, ["record", str(probe_id), "--confidence", "95", "--pick", "b"])
        out = json.loads(capsys.readouterr().out)

        assert code == 1
        assert "error" in out
        assert len(asks_rows(db)) == 1

    def test_a_pick_beyond_the_stored_options_is_rejected_without_a_write(
        self, db: Path, capsys
    ):
        probe_id = stored_probe(db)  # three options: a-c

        code = run(db, ["record", str(probe_id), "--confidence", "95", "--pick", "d"])
        out = json.loads(capsys.readouterr().out)

        assert code == 1
        assert "error" in out
        assert asks_rows(db) == []

    def test_an_unknown_probe_id_is_rejected(self, db: Path, capsys):
        Store(db).close()

        code = run(db, ["record", "999", "--confidence", "95", "--pick", "a"])
        out = json.loads(capsys.readouterr().out)

        assert code == 1
        assert "error" in out

    def test_answering_needs_both_confidence_and_pick(self, db: Path, capsys):
        probe_id = stored_probe(db)

        with pytest.raises(SystemExit):
            run(db, ["record", str(probe_id), "--pick", "a"])
        assert asks_rows(db) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_serve_record.py::TestRecord -v`
Expected: FAIL — `argument command: invalid choice: 'record'`.

- [ ] **Step 3: Implement `record` in `cli.py`**

1. Extend the ask import line:

```python
from grill.ask import (
    CONFIDENCES,
    ERROR,
    FAILED,
    PASSED,
    PREMISE_REJECTED,
    SKIPPED,
    Console,
    _unservable,
    ask as _ask,
    grade,
    resolution,
)
```

Add `import sqlite3` to the stdlib imports.

2. Add the record parser in `main`, after the serve parser:

```python
    record_parser = sub.add_parser(
        "record", help="record an answer to a probe served elsewhere"
    )
    record_parser.add_argument("probe_id", type=int)
    record_parser.add_argument("--confidence", type=int, choices=CONFIDENCES)
    record_parser.add_argument("--pick", choices=list(LETTERS[:MAX_UI_OPTIONS]))
    record_parser.add_argument("--skip", action="store_true")
    record_parser.add_argument("--wrong", action="store_true")
    record_parser.add_argument("--objection")
```

(Import `LETTERS` from `grill.ask` in the same import block.)

3. Add dispatch after the serve dispatch:

```python
    if args.command == "record":
        return _record(args, record_parser, store_factory)
```

4. Implement `_record` and its error helper above `main`:

```python
def _fail(message: str) -> int:
    """A domain error Claude can parse: JSON on stdout, non-zero exit, no write."""
    print(json.dumps({"error": message}))
    return 1


def _record(args: argparse.Namespace, parser: argparse.ArgumentParser, store_factory) -> int:
    """Record one answer non-interactively. Exactly one of pick / skip / wrong.

    Flag misuse is argparse's problem (usage error, exit 2); everything about
    the stored data — unknown id, already answered, letter out of range — is a
    JSON error, because that is the half Claude cannot know before calling.
    """
    if args.skip and args.wrong:
        parser.error("--skip and --wrong are mutually exclusive")
    if (args.skip or args.wrong) and args.pick is not None:
        parser.error("--pick only makes sense when answering")
    if not (args.skip or args.wrong) and (args.confidence is None or args.pick is None):
        parser.error("answering needs both --confidence and --pick")
    if args.objection is not None and not args.wrong:
        parser.error("--objection only makes sense with --wrong")

    with store_factory() as store:
        pending = store.probe_by_id(args.probe_id)
        if pending is None:
            return _fail(f"no servable probe with id {args.probe_id}")
        if _unservable(pending):
            return _fail(
                f"probe {args.probe_id} is malformed; `serve` records those as errors"
            )

        if args.skip:
            interrogation = resolution(pending, SKIPPED, confidence=args.confidence)
        elif args.wrong:
            interrogation = resolution(
                pending,
                PREMISE_REJECTED,
                confidence=args.confidence,
                objection=args.objection,
            )
        else:
            try:
                interrogation = grade(pending, args.confidence, args.pick)
            except ValueError as exc:
                return _fail(str(exc))

        try:
            store.record_ask(interrogation)
        except sqlite3.IntegrityError:
            # UNIQUE(probe_id): the row is permanent, so a second record is a
            # refusal, not an overwrite.
            return _fail(f"probe {args.probe_id} was already answered")

    out: dict[str, object] = {"outcome": interrogation.outcome}
    if interrogation.outcome in (PASSED, FAILED):
        out["explanation"] = pending.explanation
    print(json.dumps(out))
    return 0
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/grill/cli.py tests/test_serve_record.py
git commit -m "feat: grill record, the non-interactive answer path"
```

---

### Task 6: The `/grill` skill

**Files:**
- Create: `~/.claude/skills/grill/SKILL.md` (user-level, outside the repo — `/grill` must work in any project)
- Create: `docs/superpowers/skill/SKILL.md` (the same content, committed so the repo carries the skill's source of truth)

**Interfaces:**
- Consumes: `grill serve --json` and `grill record` exactly as Tasks 4–5 shaped them.
- Produces: the `/grill` slash command.

- [ ] **Step 1: Write the skill file**

Create `docs/superpowers/skill/SKILL.md` in the repo with this exact content, then copy it to `~/.claude/skills/grill/SKILL.md` (create the directory):

````markdown
---
name: grill
description: Serve the next pending grill probe — a multiple-choice question about code the developer recently shipped — using the native question UI. Use when the user types /grill.
---

# Serving a grill probe

Grill quizzes the developer on mechanisms they shipped without fully
understanding. You are the delivery surface only: you serve the question and
relay the answer. You never grade, never guess, and never see the answer key.

## Hard rules

- Never open or query grill's database directly. The two subcommands below are
  the entire interface.
- Never speculate about which option is correct — not in text, not in option
  descriptions, not before or after the pick. Grading happens in `grill record`.
- Confidence is committed in its own round, before the options round. Do not
  merge the rounds or reorder them.

## Flow

1. Run:

   ```
   cd ~/projects/grill && uv run grill serve --json
   ```

   If the output is `{"pending": null}`, say there is nothing pending and stop.

2. Render the probe as markdown: a context line built from `topic` and
   `created_at` (e.g. `from 2026-07-21 · idempotency of the retry path`), then
   the question, then the options lettered a), b), c), d).

3. **Round 1 — confidence.** Ask a native question: "How confident are you that
   you'll get this right?" with options `95`, `70`, `40`, and `Skip`. In the
   option descriptions, note that if the question's premise is wrong (it
   misreads what happened), they can say so via "Other" — e.g. type `wrong:
   <what's off>`.

4. **Round 2 — the pick.** Ask a native question with one option per stored
   option: the letter as the label, the full option text as the description.
   Note in a description that "Other" still accepts a skip or `wrong: ...`.

5. Record the result (always from `~/projects/grill`):
   - Picked letter L with confidence C:
     `uv run grill record <probe_id> --confidence C --pick L`
   - Skipped in round 1: `uv run grill record <probe_id> --skip`
   - Skipped in round 2: `uv run grill record <probe_id> --skip --confidence C`
   - Premise rejected: `uv run grill record <probe_id> --wrong --objection
     "<their words>"` (add `--confidence C` if they had already committed one;
     omit `--objection` if they gave no reason).

   Show the result: ✓ or ✗ from `outcome`, then the `explanation` verbatim. If
   the command prints `{"error": ...}`, show the error and stop — do not retry
   with different flags.

6. Run `serve` again. If another probe is pending, say so and offer to
   continue — do not auto-serve it.
````

- [ ] **Step 2: Verify the deployed copy**

Run: `diff docs/superpowers/skill/SKILL.md ~/.claude/skills/grill/SKILL.md && head -5 ~/.claude/skills/grill/SKILL.md`
Expected: no diff output from `diff` (exit 0), and the frontmatter starting with `---` / `name: grill`.

- [ ] **Step 3: End-to-end smoke against a throwaway database**

Run (note `GRILL_HOME` — never the real database):

```bash
cd ~/projects/grill
export GRILL_HOME=$(mktemp -d)
uv run grill serve --json
```

Expected: `{"pending": null}` — a fresh home has no probes; the point is that the installed entry point resolves the subcommand. Then `unset GRILL_HOME`.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/skill/SKILL.md
git commit -m "feat: /grill skill — Claude-native delivery for pending probes"
```

---

## Self-Review Notes

Checked against the spec:

- **Blind serve** — Task 4 asserts on raw stdout that `correct` and the explanation text never appear. ✓
- **Serve consumes nothing / Ctrl-C parity** — Task 4 `test_consumes_nothing`. ✓
- **Empty queue `{"pending": null}`, exit 0** — Task 4. ✓
- **Malformed row → `error` recorded, advance** — Task 4 (`resolution(pending, ERROR)` mirrors `ask()`'s policy). ✓
- **>4-option row skipped in the query, left pending** — Task 3 (`json_valid` guard so malformed JSON still surfaces) + Task 4 end-to-end. ✓
- **Record: grade / skip / wrong, JSON errors, no partial writes** — Task 5; `record_ask`'s existing transaction plus UNIQUE(probe_id) provide atomicity. ✓
- **Pure (pending, confidence, pick) → Interrogation in `ask.py`** — Task 2 `grade`, unit-tested in the zero-spend scripted style; `ask()` refactored to call it so grading lives in one place. ✓
- **Generation gate 3–4, prompt text updated** — Task 1 (`PROMPT` interpolates the constants; `CORRECTION` edited by hand). ✓
- **Skill: on-demand, two rounds, wrong-via-Other, no auto-serve, no DB access** — Task 6. ✓
- **Out of scope respected** — no hooks, no regeneration of stored 5-option rows, terminal path untouched (`ask()` behavior pinned by the existing suite).

One deliberate extension beyond the spec's letter: `--wrong` accepts `--confidence`, mirroring the terminal path where `/wrong` at the pick prompt carries the committed confidence. The spec only spells this out for `--skip`, but recording the commit when it happened is the same principle.
