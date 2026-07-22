# Multiple-Choice Probes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace free-text interrogation with pure multiple choice: stage 3 generates question + options + correct index + explanation, the ask path grades mechanically with zero LLM calls, and the judge/follow-up machinery is deleted.

**Architecture:** `probe.py` gains option generation/validation/shuffle; `storage.py` gains three nullable columns via an at-open migration and stops writing `criterion_results`; `ask.py` becomes a two-prompt (confidence, letter pick) pure loop; `judge.py` is deleted with `Rubric` (topic + hypothesis only) moving to `probe.py`; a one-off `backfill.py` mints options for the single pending legacy probe.

**Tech Stack:** Python 3.12, stdlib only, sqlite3, pytest (run via `uv run pytest`), LLM via the injected `complete` from `grill/llm.py`.

**Spec:** `docs/superpowers/specs/2026-07-22-multiple-choice-probes-design.md`

## Global Constraints

- 3–5 options, pairwise distinct after strip, exactly one correct; no "all of the above" / "none of the above" (prompt-only, not a gate).
- `correct` a valid index; `explanation` non-empty; existing compound-question gates on the stem (question-mark count, `SECOND_QUESTION` regex) kept.
- `MAX_ATTEMPTS` unchanged (3). Rejection → retry with correction, as today.
- Options shuffled **before storage**; stored `correct_idx` is post-shuffle and the stored row is the single source of truth.
- Confidence commit survives: 95/70/40 before the pick, exactly as today.
- No LLM call anywhere in the ask path. `outcome = error` survives only for malformed stored rows.
- `next_probe` adds `AND p.options IS NOT NULL`; legacy probes age out via existing TTL (7 days).
- Delete rather than strand: `judge()`, `Verdict`, `CriterionResult`, `Followup`, follow-up prompt, `criterion_results` writes all go.
- Migration at open: `ALTER TABLE` with nullable columns. The legacy `criteria` column stays (NOT NULL in existing DBs); new rows write `'[]'` into it.
- `asks.turns` stays for continuity; it is 0 (skip before pick) or 1.
- Commits: author from the repo's git config, no Co-Authored-By trailer, terse messages.
- Test command: `uv run pytest` (whole suite must be green at the end of every task).

## File Structure

| File | Change |
|---|---|
| `src/grill/probe.py` | Rewrite prompt + parsing for MC; add `validate_choices`, `shuffle_choices`; later hosts `Rubric` |
| `src/grill/judge.py` | Task 1: `criteria` gets default `()`. Task 4: deleted |
| `src/grill/storage.py` | New columns + migration, `add_probe`/`next_probe`/`record_ask` updates, `legacy_probes`/`set_choices` |
| `src/grill/ask.py` | Two-prompt MC loop, no judge/followup, `PendingProbe`/`AnswerTurn` reshaped |
| `src/grill/cli.py` | Unchanged wiring (ask signature shrinks) |
| `src/grill/triage.py`, `src/grill/seed.py` | Prompt-only mechanism preference |
| `src/grill/backfill.py` | New: one-off legacy probe conversion, `python -m grill.backfill` |
| Tests | `test_probe.py`, `test_ask.py`, `test_storage.py`, `test_cli.py`, `test_capture.py` updated; `test_judge.py`, `test_judge_calibration.py` deleted; `test_backfill.py` new; smoke tests inspected and updated/deleted as they reference dead machinery |

---

### Task 1: Stage 3 generates multiple choice

**Files:**
- Modify: `src/grill/probe.py`
- Modify: `src/grill/judge.py` (one-line default, temporary until Task 4)
- Test: `tests/test_probe.py`
- Modify: `tests/test_capture.py`, `tests/conftest.py` if they construct `Probe` (grep first)

**Interfaces:**
- Produces: `Probe(question: str, options: tuple[str, ...], correct_idx: int, explanation: str, rubric: Rubric, cost_usd, duration_ms)`; `validate_choices(parsed: dict, question: str) -> tuple[tuple[str, ...], int, str]`; `shuffle_choices(options, correct, rng=None) -> tuple[tuple[str, ...], int]`; `parse_probe(seed, completion, *, rng=None)`. `Followup`/`followup` untouched this task (deleted in Task 4).
- Consumes: `Rubric` from `grill.judge`, constructed without `criteria`.

- [ ] **Step 1: Give `Rubric.criteria` a default** in `src/grill/judge.py`:

```python
    criteria: tuple[str, ...] = ()
```

- [ ] **Step 2: Write the failing tests.** Rewrite `tests/test_probe.py`'s generation half (keep `TestFollowup` and `SEED`/`DIALOGUE`/`completion` helpers as-is). Replace the `response()` helper and the rubric/one-question/regeneration classes:

```python
def response(**overrides) -> str:
    body = {
        "question": "What does GitHub create when `#4923` follows a non-word character?",
        "options": [
            "A closing keyword that closes the issue on merge",
            "A cross-reference event on the issue's timeline",
            "A label linking the PR to the milestone",
        ],
        "correct": 1,
        "explanation": "GitHub only autolinks `#N` after a non-word character, producing a cross-reference event.",
    }
    body.update(overrides)
    return json.dumps(body)


class SeededRng:
    """random.Random with a fixed seed, for deterministic shuffle assertions."""


class TestChoices:
    def test_accepts_a_well_formed_probe(self):
        parsed = parse_probe(SEED, completion(response()))
        assert len(parsed.options) == 3
        assert parsed.options[parsed.correct_idx].startswith("A cross-reference")
        assert parsed.explanation.startswith("GitHub only autolinks")

    def test_rejects_fewer_than_three_options(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(options=["one", "two"], correct=0)))

    def test_rejects_more_than_five_options(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(options=[f"option {n}" for n in range(6)], correct=0)))

    def test_rejects_duplicate_options_after_strip(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(options=["same", "  same  ", "other"], correct=0)))

    def test_rejects_an_out_of_range_correct_index(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(correct=3)))

    def test_rejects_a_boolean_correct(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(correct=True)))

    def test_rejects_an_empty_explanation(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(explanation="  ")))

    def test_rejects_a_missing_question(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(question="  ")))

    def test_carries_the_hypothesis_into_the_rubric(self):
        parsed = parse_probe(SEED, completion(response()))
        assert parsed.rubric.hypothesis == SEED.hypothesis
        assert parsed.rubric.topic == SEED.topic


class TestShuffle:
    def test_correct_idx_always_names_the_option_the_model_marked_correct(self):
        """The property the spec states: shuffle moves positions, never truth."""
        import random

        for seed_value in range(50):
            parsed = parse_probe(
                SEED, completion(response()), rng=random.Random(seed_value)
            )
            assert parsed.options[parsed.correct_idx] == (
                "A cross-reference event on the issue's timeline"
            )

    def test_the_shuffle_actually_shuffles(self):
        import random

        orders = {
            parse_probe(SEED, completion(response()), rng=random.Random(s)).options
            for s in range(20)
        }
        assert len(orders) > 1
```

Keep `TestOneQuestion` (compound-stem cases now passed via `response(question=...)`) and `TestRegeneration`/`TestPrompt` shapes, updating `response()` usage; in `TestRegeneration`, rejections still show the offending question and retry within `MAX_ATTEMPTS`. Drop `TestRubricComesFirst.test_rejects_a_probe_with_no_criteria`.

- [ ] **Step 3: Run to verify failure:** `uv run pytest tests/test_probe.py -x -q` — FAIL (`Probe` has no `options`).

- [ ] **Step 4: Implement in `src/grill/probe.py`:**

`import random`; `PROBE_KEYS = ("question", "options", "correct", "explanation")`; add `MIN_OPTIONS = 3`, `MAX_OPTIONS = 5`.

```python
@dataclass(frozen=True)
class Probe:
    """One multiple-choice question: stem, shuffled options, and the answer key."""

    question: str
    options: tuple[str, ...]
    correct_idx: int
    explanation: str
    rubric: Rubric
    cost_usd: float | None = None
    duration_ms: int | None = None
```

Replace `PROMPT` (keep `build_prompt` as-is):

```python
PROMPT = """\
You are stage 3 of `grill`. Everything has already been decided except the
question itself: the session was triaged, the moment selected, and a hypothesis
formed about what this developer accepted without fully understanding.

Produce ONE multiple-choice question that tests the mechanism at the core of
that hypothesis.

## The hypothesis you are testing

{hypothesis}

## The topic

{topic}

## What shipped

{decision}

## What the developer said

{quotes}

## The session

{rendered}

## What to produce

`question` — ONE question about the mechanism itself: what the API, tool,
configuration, or algorithm does or requires. Never a retelling of the
conversation: no "you asked", "you said", "Claude did", or any conversational
narrative. Ground it in the session's actual artifact — name the file, flag, or
identifier that shipped — but ask how the mechanism works, not what happened.
It must be one question: no "and", no "also", no parts.

`options` — {min_options} to {max_options} one-line answers, exactly ONE of
them correct. Every wrong option must describe a plausible wrong MECHANISM —
something a developer who half-understood the decision would believe. The
dangerous failure is a fluent answer describing a different mechanism, and the
distractors are where you catch it. No "all of the above" or "none of the
above". No option may be a joke or an obvious throwaway.

`correct` — the zero-based index of the correct option, as you listed them.

`explanation` — 1 to 3 sentences shown after the developer picks, right or
wrong: why the correct option is correct, in terms of the mechanism.

Do not be clever or arch. A developer who has been coding for six hours reads
this as a few lines of text, and a question that performs is a question that
gets skipped.

## Respond

One JSON object, nothing else:

{{"question": "...", "options": ["...", "..."], "correct": 0, "explanation": "..."}}
"""
```

`build_prompt` gains `min_options=MIN_OPTIONS, max_options=MAX_OPTIONS` in the `.format(...)` call.

Replace `CORRECTION` (generalized beyond the compound-stem case):

```python
CORRECTION = """\

## Your previous attempt was rejected

You returned this question:

{question}

It was rejected: {reason}

Fix exactly what the rejection names, changing nothing else about your
approach. The rules: one question (a second clause after a comma — "…, and how
would you…" — is a second question; drop it entirely), 3 to 5 one-line options
with exactly one correct, no duplicates, `correct` a valid zero-based index,
and a non-empty `explanation`.

Return the same JSON shape.
"""
```

Add validation and shuffle:

```python
def validate_choices(parsed: dict, question: str) -> tuple[tuple[str, ...], int, str]:
    """The option gates, shared with the backfill because they are the same rules."""
    raw = parsed.get("options")
    options = (
        tuple(o.strip() for o in raw if isinstance(o, str) and o.strip())
        if isinstance(raw, list)
        else ()
    )
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ProbeRejected(
            f"probe needs {MIN_OPTIONS}-{MAX_OPTIONS} options, got {len(options)}",
            question,
        )
    if len(set(options)) != len(options):
        raise ProbeRejected("probe has duplicate options", question)

    correct = parsed.get("correct")
    if isinstance(correct, bool) or not isinstance(correct, int) or not 0 <= correct < len(options):
        raise ProbeRejected(f"'correct' is not a valid option index: {correct!r}", question)

    explanation = parsed.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ProbeRejected("probe has no explanation", question)

    return options, correct, explanation.strip()


def shuffle_choices(
    options: tuple[str, ...], correct: int, rng: random.Random | None = None
) -> tuple[tuple[str, ...], int]:
    """Shuffle at storage time, not display time.

    The stored row is the single source of truth for what position was shown,
    so `correct_idx` must be minted post-shuffle, here, once.
    """
    order = list(range(len(options)))
    (rng or random).shuffle(order)
    return tuple(options[i] for i in order), order.index(correct)
```

Rewrite `parse_probe`:

```python
def parse_probe(
    seed: Seed, completion: Completion, *, rng: random.Random | None = None
) -> Probe:
    """Read a probe, enforcing the structural gates the design states as gates.

    All rejections are structural rather than qualitative. Whether a question is
    *good* is settled by the yes-rate, not here; whether it is one mechanically
    gradable multiple-choice question is settled here.
    """
    parsed = extract_json_object(completion.text, salvage_keys=PROBE_KEYS)

    question = parsed.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ProbeRejected("probe has no question")
    question = question.strip()

    reject_if_compound(question)

    options, correct, explanation = validate_choices(parsed, question)
    shuffled, correct_idx = shuffle_choices(options, correct, rng)

    return Probe(
        question=question,
        options=shuffled,
        correct_idx=correct_idx,
        explanation=explanation,
        rubric=Rubric(topic=seed.topic, hypothesis=seed.hypothesis),
        cost_usd=completion.cost_usd,
        duration_ms=completion.duration_ms,
    )
```

Update the module docstring (criteria/judge framing → multiple choice, verdict mechanical). `probe()` loop unchanged.

- [ ] **Step 5: Fix other `Probe(...)` constructions:** `grep -rn "Probe(" tests/ src/` and update `tests/test_capture.py` (and any smoke test) stubs to the new signature, e.g. `Probe(question="…?", options=("a", "b", "c"), correct_idx=0, explanation="because.", rubric=Rubric(topic="t", hypothesis="h"))`.

- [ ] **Step 6: Run the whole suite:** `uv run pytest -q` — expected PASS (storage still writes `rubric.criteria`, now `()` → `'[]'`; its own fixtures pass `criteria` explicitly and still work — if any storage/ask test breaks on the `Probe` shape, fix its fixture, not the code).

- [ ] **Step 7: Commit:** `git add -A && git commit -m "feat: stage 3 generates multiple-choice probes"`

---

### Task 2: Storage — columns, migration, writes, filter

**Files:**
- Modify: `src/grill/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `probes` table with nullable `options` (JSON TEXT), `correct_idx` (INTEGER), `explanation` (TEXT); `add_probe` persisting them; `next_probe` never returning a row with `options IS NULL`. `PendingProbe` shape unchanged until Task 3.
- Consumes: `Probe` from Task 1.

- [ ] **Step 1: Write the failing tests.** In `tests/test_storage.py`, replace `a_probe()`:

```python
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
        rubric=Rubric(
            topic="idempotency of the retry path",
            hypothesis="the developer accepted the key without knowing what it dedupes against",
        ),
        cost_usd=0.13,
    )
```

Replace `test_probe_stores_criteria_only` with:

```python
def test_probe_stores_its_choices(store: Store):
    store.record_session(
        session_id="0198e4f1",
        transcript_path="/tmp/0198e4f1.jsonl",
        cwd=None,
        git_branch=None,
        verdict="ask",
    )
    probe_id = store.add_probe(store.add_seed(a_seed()), a_probe())
    row = store.conn.execute(
        "SELECT question, options, correct_idx, explanation FROM probes WHERE id = ?",
        (probe_id,),
    ).fetchone()
    import json

    assert json.loads(row["options"]) == list(a_probe().options)
    assert row["correct_idx"] == 0
    assert row["explanation"].startswith("Within the dedupe window")
```

In `test_carries_the_whole_rubric`, drop the `criteria` assertion (topic/hypothesis/question assertions stay). Add a migration class:

```python
LEGACY_SCHEMA = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, transcript_path TEXT NOT NULL, cwd TEXT,
    git_branch TEXT, verdict TEXT NOT NULL, signal TEXT, topic TEXT,
    cost_usd REAL, triaged_at TEXT NOT NULL
);
CREATE TABLE seeds (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn INTEGER NOT NULL, signal TEXT NOT NULL, topic TEXT NOT NULL,
    quotes TEXT NOT NULL, refs TEXT NOT NULL, decision TEXT NOT NULL,
    hypothesis TEXT NOT NULL, cost_usd REAL, created_at TEXT NOT NULL
);
CREATE TABLE probes (
    id INTEGER PRIMARY KEY, seed_id INTEGER NOT NULL REFERENCES seeds(id),
    question TEXT NOT NULL, criteria TEXT NOT NULL, cost_usd REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE asks (
    id INTEGER PRIMARY KEY, probe_id INTEGER NOT NULL UNIQUE REFERENCES probes(id),
    asked_at TEXT NOT NULL, confidence INTEGER, outcome TEXT NOT NULL,
    objection TEXT, turns INTEGER NOT NULL, cost_usd REAL, completed_at TEXT
);
CREATE TABLE answers (
    id INTEGER PRIMARY KEY, ask_id INTEGER NOT NULL REFERENCES asks(id),
    turn INTEGER NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE criterion_results (
    id INTEGER PRIMARY KEY, answer_id INTEGER NOT NULL REFERENCES answers(id),
    criterion TEXT NOT NULL, met INTEGER NOT NULL
);
"""


class TestMigration:
    """Opening a legacy database must upgrade it in place without touching rows."""

    def legacy_db(self, tmp_path: Path) -> Path:
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO sessions (session_id, transcript_path, verdict, triaged_at)"
            " VALUES ('legacy', '/tmp/legacy.jsonl', 'ask', '2026-07-21T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO seeds (session_id, turn, signal, topic, quotes, refs,"
            " decision, hypothesis, created_at) VALUES ('legacy', 1, 'asked_why',"
            " 'issue linking', '[]', '[]', 'used Refs #4923',"
            " 'accepted autolinking without knowing the non-word rule',"
            " '2026-07-21T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO probes (seed_id, question, criteria, created_at)"
            " VALUES (1, 'What did the swap cause?',"
            " '[\"names the cross-reference event\"]', ?)",
            (iso_days_ago(1),),
        )
        conn.commit()
        conn.close()
        return path

    def test_migration_adds_the_columns(self, tmp_path: Path):
        with Store(self.legacy_db(tmp_path)) as store:
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(probes)")}
        assert {"options", "correct_idx", "explanation"} <= cols

    def test_migration_is_idempotent(self, tmp_path: Path):
        path = self.legacy_db(tmp_path)
        with Store(path):
            pass
        with Store(path) as store:
            assert store.conn.execute("SELECT count(*) FROM probes").fetchone()[0] == 1

    def test_a_legacy_probe_is_never_served(self, tmp_path: Path):
        with Store(self.legacy_db(tmp_path)) as store:
            assert store.next_probe() is None

    def test_a_legacy_row_keeps_its_criteria(self, tmp_path: Path):
        with Store(self.legacy_db(tmp_path)) as store:
            row = store.conn.execute("SELECT criteria, options FROM probes").fetchone()
        assert "cross-reference" in row["criteria"]
        assert row["options"] is None
```

- [ ] **Step 2: Run to verify failure:** `uv run pytest tests/test_storage.py -x -q` — FAIL (no `options` column / `Probe` kwargs).

- [ ] **Step 3: Implement in `src/grill/storage.py`.** In `SCHEMA`, the `probes` table becomes:

```sql
CREATE TABLE IF NOT EXISTS probes (
    id          INTEGER PRIMARY KEY,
    seed_id     INTEGER NOT NULL REFERENCES seeds(id),
    question    TEXT NOT NULL,
    criteria    TEXT NOT NULL,
    options     TEXT,
    correct_idx INTEGER,
    explanation TEXT,
    cost_usd    REAL,
    created_at  TEXT NOT NULL
);
```

In `Store.__init__`, after `executescript(SCHEMA)` and before `commit()`, call `self._migrate()`:

```python
    def _migrate(self) -> None:
        """Bring a pre-multiple-choice database up to the current schema.

        `CREATE TABLE IF NOT EXISTS` cannot add columns to a table that already
        exists, so databases created before the choice columns need each one
        added by hand. Nullable, so legacy rows are untouched.
        """
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(probes)")}
        for name, kind in (
            ("options", "TEXT"),
            ("correct_idx", "INTEGER"),
            ("explanation", "TEXT"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE probes ADD COLUMN {name} {kind}")
```

Rewrite `add_probe`:

```python
    def add_probe(self, seed_id: int, probe: Probe) -> int:
        """Store the question, its shuffled options, and the answer key.

        `criteria` is written as an empty list: the column is NOT NULL in every
        database that predates multiple choice, and rewriting the table to relax
        it risks the live data for no query we run. Legacy rows keep theirs.
        """
        cursor = self.conn.execute(
            "INSERT INTO probes"
            " (seed_id, question, criteria, options, correct_idx, explanation,"
            "  cost_usd, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seed_id,
                probe.question,
                json.dumps([]),
                json.dumps(list(probe.options)),
                probe.correct_idx,
                probe.explanation,
                probe.cost_usd,
                _now(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)
```

In `next_probe`, add `" AND p.options IS NOT NULL"` to the WHERE clause (after the cutoff condition) with the comment `# Legacy free-text probes are never served; they age out via the TTL.` — keep the returned `PendingProbe` exactly as it is for now (`criteria=tuple(json.loads(row["criteria"]))` still works: new rows carry `'[]'`).

- [ ] **Step 4: Run the whole suite:** `uv run pytest -q` — expected PASS.

- [ ] **Step 5: Commit:** `git add -A && git commit -m "feat: probes store options, correct_idx, explanation; legacy rows never served"`

---

### Task 3: The ask flow — pick a letter

**Files:**
- Modify: `src/grill/ask.py` (rewrite), `src/grill/storage.py` (`next_probe`, `record_ask`), `src/grill/cli.py` (fixture-level only)
- Test: `tests/test_ask.py` (rewrite), `tests/test_storage.py`, `tests/test_cli.py`; inspect `tests/test_ask_smoke.py` (delete if it exercises the free-text judge path — the ask path no longer spends)

**Interfaces:**
- Produces: `PendingProbe(probe_id, question, options: tuple[str, ...], correct_idx: int | None, explanation: str, rubric: Rubric, created_at)`; `AnswerTurn(turn, question, answer)` (no `criteria`); `ask(pending, console) -> Interrogation` (no judge/followup params). `Interrogation` shape unchanged.
- Consumes: `Rubric` (from `grill.judge` until Task 4), storage rows with the Task 2 columns.

- [ ] **Step 1: Rewrite `tests/test_ask.py`:**

```python
"""Tests for the multiple-choice interrogation.

A scripted console stands in for the terminal; there is no model anywhere in
this path, so every branch runs at zero spend by construction rather than by
injection. What these pin down is the exits: correct pick, wrong pick, skip at
each prompt, /wrong at each prompt, invalid-then-valid input, and the one error
that survives — a malformed stored row.
"""

from __future__ import annotations

import pytest

from grill.ask import (
    ERROR,
    FAILED,
    PASSED,
    PREMISE_REJECTED,
    SKIPPED,
    PendingProbe,
    ask,
)
from grill.judge import Rubric

RUBRIC = Rubric(
    topic="linking a PR to the issue it fixes",
    hypothesis="the developer accepted Refs #4923 without knowing what GitHub creates",
)

PENDING = PendingProbe(
    probe_id=7,
    question="What did swapping `GH#4923` for `Refs #4923` cause GitHub to create?",
    options=(
        "A closing keyword that closes #4923 on merge",
        "A cross-reference event on #4923's timeline",
        "A label linking the PR to the milestone",
    ),
    correct_idx=1,
    explanation="GitHub only autolinks `#N` when `#` follows a non-word character.",
    rubric=RUBRIC,
    created_at="2026-07-21T09:00:00+00:00",
)


class ScriptedConsole:
    """Replays a list of inputs and remembers everything it was shown."""

    def __init__(self, inputs: list[str]) -> None:
        self.inputs = list(inputs)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def show(self, text: str) -> None:
        self.shown.append(text)

    def prompt(self, text: str) -> str:
        self.prompts.append(text)
        if not self.inputs:
            raise AssertionError(f"console ran out of scripted input at: {text!r}")
        return self.inputs.pop(0)


class TestVerdicts:
    def test_the_correct_pick_passes(self):
        console = ScriptedConsole(["95", "b"])

        result = ask(PENDING, console)

        assert result.outcome == PASSED
        assert result.confidence == 95
        assert len(result.turns) == 1
        assert result.turns[0].answer == PENDING.options[1]
        assert result.reasoning == PENDING.explanation

    def test_a_wrong_pick_fails(self):
        console = ScriptedConsole(["95", "a"])

        result = ask(PENDING, console)

        assert result.outcome == FAILED
        assert result.turns[0].answer == PENDING.options[0]
        assert result.reasoning == PENDING.explanation

    def test_the_pick_is_case_insensitive(self):
        result = ask(PENDING, ScriptedConsole(["95", "B"]))

        assert result.outcome == PASSED

    def test_the_explanation_is_shown_on_a_pass_with_a_check(self):
        console = ScriptedConsole(["95", "b"])

        ask(PENDING, console)

        assert any(
            text.startswith("✓") and PENDING.explanation in text for text in console.shown
        )

    def test_the_explanation_is_shown_on_a_fail_with_a_cross(self):
        console = ScriptedConsole(["95", "c"])

        ask(PENDING, console)

        assert any(
            text.startswith("✗") and PENDING.explanation in text for text in console.shown
        )

    def test_nothing_is_spent(self):
        result = ask(PENDING, ScriptedConsole(["95", "b"]))

        assert result.cost_usd == 0.0


class TestDisplay:
    def test_the_context_line_carries_the_topic(self):
        console = ScriptedConsole(["95", "b"])

        ask(PENDING, console)

        assert any("linking a PR to the issue it fixes" in text for text in console.shown)

    def test_every_option_is_shown_with_its_letter(self):
        console = ScriptedConsole(["95", "b"])

        ask(PENDING, console)

        listing = "\n".join(console.shown)
        for letter, option in zip("abc", PENDING.options):
            assert f"{letter}) {option}" in listing

    def test_the_pick_prompt_names_the_letter_range(self):
        console = ScriptedConsole(["95", "b"])

        ask(PENDING, console)

        assert any("[a-c]" in p for p in console.prompts)


class TestSkip:
    def test_skip_at_the_confidence_prompt(self):
        result = ask(PENDING, ScriptedConsole([""]))

        assert result.outcome == SKIPPED
        assert result.confidence is None
        assert result.turns == ()

    def test_skip_at_the_pick_prompt(self):
        result = ask(PENDING, ScriptedConsole(["95", ""]))

        assert result.outcome == SKIPPED
        assert result.confidence == 95
        assert result.turns == ()


class TestWrong:
    def test_wrong_at_the_confidence_prompt_with_an_objection(self):
        console = ScriptedConsole(["/wrong", "I never used Refs, that was the agent"])

        result = ask(PENDING, console)

        assert result.outcome == PREMISE_REJECTED
        assert result.objection == "I never used Refs, that was the agent"
        assert result.confidence is None

    def test_wrong_without_an_objection(self):
        result = ask(PENDING, ScriptedConsole(["/wrong", ""]))

        assert result.outcome == PREMISE_REJECTED
        assert result.objection is None

    def test_wrong_at_the_pick_prompt(self):
        result = ask(PENDING, ScriptedConsole(["70", "/wrong", "the question misreads the diff"]))

        assert result.outcome == PREMISE_REJECTED
        assert result.confidence == 70
        assert result.objection == "the question misreads the diff"


class TestInvalidInput:
    def test_confidence_reprompts_on_anything_else(self):
        result = ask(PENDING, ScriptedConsole(["100", "95", "b"]))

        assert result.confidence == 95

    def test_the_pick_reprompts_with_a_hint_on_an_unknown_letter(self):
        console = ScriptedConsole(["95", "z", "b"])

        result = ask(PENDING, console)

        assert result.outcome == PASSED
        assert any("a-c" in text for text in console.shown)

    def test_the_pick_reprompts_on_a_multi_character_answer(self):
        result = ask(PENDING, ScriptedConsole(["95", "ab", "b"]))

        assert result.outcome == PASSED


class TestMalformed:
    """The one place `error` survives: a stored row the ask cannot serve."""

    @pytest.mark.parametrize(
        "broken",
        [
            {"options": ()},
            {"correct_idx": None},
            {"correct_idx": 9},
            {"explanation": "  "},
        ],
    )
    def test_a_malformed_row_is_an_error_before_any_prompt(self, broken):
        from dataclasses import replace

        console = ScriptedConsole([])

        result = ask(replace(PENDING, **broken), console)

        assert result.outcome == ERROR
        assert console.prompts == []
```

- [ ] **Step 2: Run to verify failure:** `uv run pytest tests/test_ask.py -x -q` — FAIL (`PendingProbe` has no `options`).

- [ ] **Step 3: Rewrite `src/grill/ask.py`:**

```python
"""One probe, asked and graded mechanically.

Pure logic. The console is injected, exactly as `capture_session` injects its
stages, so the whole flow is drivable by a scripted console. No argparse, no
terminal control codes, no TTY — those belong to `cli.py`, and keeping them out
is what leaves the delivery question open.

There is no model call anywhere in this path. Picking an option IS the answer:
the verdict is `pick == correct_idx`, the explanation was written at generation
time, and the only `error` outcome left is a stored row too malformed to serve.
That is a property of the design, not of careful coding — a judge cannot be
slow, expensive, or cowardly if there is no judge.
"""

from __future__ import annotations

from dataclasses import dataclass

from typing import Protocol

from grill.judge import Rubric

# Written to a TEXT column and grouped on by hand later, so the strings are the
# schema. `premise_rejected` is its own outcome rather than a flavour of skip:
# it is the zealot rate, and a rate you cannot query is a rate nobody checks.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
PREMISE_REJECTED = "premise_rejected"
ERROR = "error"

# Three coarse buckets, not a slider. The number exists to be quoted back at
# resurfacing — "you said 95%" — and a 0-100 field invites a hedge.
CONFIDENCES = (95, 70, 40)

WRONG = "/wrong"

# Option letters, and therefore the hard cap on how many options a stored row
# may carry and still be served.
LETTERS = "abcde"


class Console(Protocol):
    """Everything the interrogation needs from the outside world."""

    def show(self, text: str) -> None: ...

    def prompt(self, text: str) -> str: ...


@dataclass(frozen=True)
class PendingProbe:
    """A stored probe, ready to be asked.

    Lives here rather than in `storage.py` so that the import runs
    storage -> ask, matching how storage already imports `Probe` and `Seed` from
    the modules that produce them, and avoiding the cycle the other direction
    would create.

    `correct_idx` is `int | None` because the row is trusted nowhere: a legacy
    or mangled row surfaces as `None` or an out-of-range index, and `ask`
    records `error` instead of guessing.
    """

    probe_id: int
    question: str
    options: tuple[str, ...]
    correct_idx: int | None
    explanation: str
    rubric: Rubric
    created_at: str


@dataclass(frozen=True)
class AnswerTurn:
    """One developer turn: the question, and the option text they chose."""

    turn: int
    question: str
    answer: str


@dataclass(frozen=True)
class Interrogation:
    """Everything that happened, ready to be stored. No database in sight."""

    probe_id: int
    outcome: str
    confidence: int | None
    objection: str | None
    turns: tuple[AnswerTurn, ...]
    cost_usd: float | None
    reasoning: str


CONFIDENCE_PROMPT = "confidence   [95 | 70 | 40]   ·   enter = skip   ·   /wrong"
OBJECTION_PROMPT = "what's wrong with it? (enter to skip)"
CONFIDENCE_HINT = "type 95, 70, or 40."
MALFORMED = "this probe's stored options are unusable; recording an error."


def context_line(pending: PendingProbe) -> str:
    """One line of orientation above the question.

    Without it the developer reads a question about work they cannot place, which
    is the version of this tool that feels like a quiz. `created_at` is the
    probe's, which is the session's within a few seconds of it.
    """
    when = pending.created_at[:10]
    return f"from {when} · {pending.rubric.topic}"


def render_options(options: tuple[str, ...]) -> str:
    return "\n".join(f"  {LETTERS[i]}) {option}" for i, option in enumerate(options))


def pick_prompt(count: int) -> str:
    return f"pick   [a-{LETTERS[count - 1]}]   ·   enter = skip   ·   /wrong"


def pick_hint(count: int) -> str:
    return f"type a single letter, a-{LETTERS[count - 1]}."


def _unservable(pending: PendingProbe) -> bool:
    """A row the ask cannot honestly grade.

    Storage already filters `options IS NULL`, so what arrives here failed to
    parse or carries an index that names no option. Grading either would invent
    a verdict, which is worse than admitting the row is broken.
    """
    return (
        len(pending.options) < 2
        or len(pending.options) > len(LETTERS)
        or pending.correct_idx is None
        or not 0 <= pending.correct_idx < len(pending.options)
        or not pending.explanation.strip()
    )


def ask(pending: PendingProbe, console: Console) -> Interrogation:
    """Run one probe to a verdict: confidence, pick, mechanical grade."""

    def done(
        outcome: str,
        *,
        confidence: int | None = None,
        objection: str | None = None,
        turns: tuple[AnswerTurn, ...] = (),
        reasoning: str = "",
    ) -> Interrogation:
        return Interrogation(
            probe_id=pending.probe_id,
            outcome=outcome,
            confidence=confidence,
            objection=objection,
            turns=turns,
            cost_usd=0.0,
            reasoning=reasoning,
        )

    def ask_objection() -> str | None:
        """Shared `/wrong` handling: prompt once for an optional reason.

        Optional because requiring an argument to escape is how you get an escape
        hatch nobody uses. The outcome is the signal; the text is a bonus.
        """
        typed = console.prompt(OBJECTION_PROMPT).strip()
        return typed or None

    if _unservable(pending):
        console.show(MALFORMED)
        return done(ERROR)

    console.show(context_line(pending))
    console.show(pending.question)
    console.show(render_options(pending.options))

    # Confidence, once, before the pick. Committing a number before answering
    # is the whole mechanism; the miscalibration signal — confident and wrong —
    # is the product.
    confidence: int | None = None
    while confidence is None:
        typed = console.prompt(CONFIDENCE_PROMPT).strip()
        if not typed:
            return done(SKIPPED)
        if typed == WRONG:
            return done(PREMISE_REJECTED, objection=ask_objection())
        if typed.isdigit() and int(typed) in CONFIDENCES:
            confidence = int(typed)
        else:
            console.show(CONFIDENCE_HINT)

    valid = LETTERS[: len(pending.options)]
    while True:
        typed = console.prompt(pick_prompt(len(pending.options))).strip().lower()
        if not typed:
            return done(SKIPPED, confidence=confidence)
        if typed == WRONG:
            return done(PREMISE_REJECTED, confidence=confidence, objection=ask_objection())
        if len(typed) == 1 and typed in valid:
            picked = valid.index(typed)
            break
        console.show(pick_hint(len(pending.options)))

    passed = picked == pending.correct_idx
    console.show(f"{'✓' if passed else '✗'} {pending.explanation}")

    return done(
        PASSED if passed else FAILED,
        confidence=confidence,
        turns=(AnswerTurn(turn=0, question=pending.question, answer=pending.options[picked]),),
        reasoning=pending.explanation,
    )
```

- [ ] **Step 4: Update `src/grill/storage.py`.** `next_probe` selects and carries the new columns:

```python
        row = self.conn.execute(
            "SELECT p.id, p.question, p.options, p.correct_idx, p.explanation,"
            " p.created_at, s.topic, s.hypothesis"
            " FROM probes p"
            " JOIN seeds s ON s.id = p.seed_id"
            " LEFT JOIN asks a ON a.probe_id = p.id"
            " WHERE a.id IS NULL AND p.created_at >= ?"
            # Legacy free-text probes are never served; they age out via the TTL.
            " AND p.options IS NOT NULL"
            " ORDER BY p.created_at DESC, p.id DESC"
            " LIMIT 1",
            (cutoff,),
        ).fetchone()

        if row is None:
            return None

        # Parsed defensively rather than trusted: a row that fails here is
        # served anyway and becomes ask's `error` outcome, which is the one
        # error the design keeps.
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

Update its docstring (rubric reassembly comment loses "criteria"; note malformed rows are served on purpose). In `record_ask`, delete the `criterion_results` `executemany` block (keep answers). Keep the transaction comment.

- [ ] **Step 5: Update the remaining tests.**
  - `tests/test_storage.py`: `an_interrogation()` loses `criteria` on `AnswerTurn`; delete `test_stores_a_row_per_criterion_per_turn`; `an_interrogation` cost becomes `0.0` and adjust the asks assertion (`row["cost_usd"] == 0.0`); add to `TestNextProbe`:

```python
    def test_carries_the_choices(self, store: Store):
        self.stored(store, session_id="one", created_at=iso_days_ago(1), topic="t")

        pending = store.next_probe()

        assert pending is not None
        assert pending.options == a_probe().options
        assert pending.correct_idx == a_probe().correct_idx
        assert pending.explanation == a_probe().explanation

    def test_a_row_with_unparseable_options_is_served_with_empty_options(self, store: Store):
        """Malformed rows surface as ask's `error`, not as silence."""
        probe_id = self.stored(store, session_id="one", created_at=iso_days_ago(1), topic="t")
        store.conn.execute(
            "UPDATE probes SET options = 'not json' WHERE id = ?", (probe_id,)
        )
        store.conn.commit()

        pending = store.next_probe()

        assert pending is not None
        assert pending.options == ()
```

  - `tests/test_cli.py`: `PENDING` gains `options=("a1", "a2", "a3"), correct_idx=0, explanation="because."`; `RUBRIC` loses `criteria`; scripted inputs become confidence + letter (e.g. `["95", "a"]`).
  - `tests/test_ask_smoke.py` and `tests/test_capture_smoke.py`: read them; delete `test_ask_smoke.py` if it drives the judge/follow-up path (nothing left to smoke — the path has no model), and update capture smoke fixtures to the new `Probe` shape if needed.
  - `tests/test_capture.py`: already updated in Task 1; re-check.

- [ ] **Step 6: Run the whole suite:** `uv run pytest -q` — expected PASS.

- [ ] **Step 7: Commit:** `git add -A && git commit -m "feat: the ask flow is a letter pick graded mechanically"`

---

### Task 4: Delete the grading machinery

**Files:**
- Delete: `src/grill/judge.py`, `tests/test_judge.py`, `tests/test_judge_calibration.py`
- Modify: `src/grill/probe.py` (host `Rubric`; drop follow-up machinery), `src/grill/ask.py`, `src/grill/storage.py` (imports; drop `criterion_results` from `SCHEMA`), `tests/test_probe.py` (drop `TestFollowup`), any other `grill.judge` importers

**Interfaces:**
- Produces: `Rubric(topic: str, hypothesis: str)` defined in `grill.probe`; no `Followup`, `followup`, `parse_followup`, `build_followup_prompt`, `FOLLOWUP_PROMPT` anywhere.
- Consumes: nothing new.

- [ ] **Step 1: Move `Rubric` into `src/grill/probe.py`** (above `Probe`), replacing the `from grill.judge import Rubric` import:

```python
@dataclass(frozen=True)
class Rubric:
    """The claim a probe tests, and the frame it was asked in.

    `hypothesis` is what makes a failure diagnosable: a wrong pick is
    attributable to a wrong hypothesis or to a right hypothesis asked badly.
    Without it a failed probe is an unactionable complaint.
    """

    topic: str
    hypothesis: str
```

- [ ] **Step 2: Delete the follow-up machinery from `src/grill/probe.py`:** `Followup`, `FOLLOWUP_PROMPT`, `build_followup_prompt`, `parse_followup`, `followup`. Delete `TestFollowup` from `tests/test_probe.py` and its `followup` import.

- [ ] **Step 3: Repoint imports.** `grep -rn "grill.judge\|grill import judge\|CriterionResult\|Verdict" src/ tests/` — every `from grill.judge import Rubric` becomes `from grill.probe import Rubric` (`ask.py`, `storage.py`, `test_ask.py`, `test_cli.py`, `test_storage.py`, `test_capture.py`, …). Then `git rm src/grill/judge.py tests/test_judge.py tests/test_judge_calibration.py`.

- [ ] **Step 4: Drop the `criterion_results` CREATE from `SCHEMA`** in `src/grill/storage.py` (legacy databases keep their table and rows; fresh ones never grow it) and update the module docstring's table count/rationale.

- [ ] **Step 5: Run the whole suite and grep for strays:** `uv run pytest -q` — PASS; `grep -rn "judge\|Followup\|criterion_results" src/grill/` returns only comments that are genuinely about history (aim for zero; rewrite comments that still narrate the judge).

- [ ] **Step 6: Commit:** `git add -A && git commit -m "refactor: delete the judge and follow-up machinery"`

---

### Task 5: Selection prompts prefer mechanisms

**Files:**
- Modify: `src/grill/triage.py` (PROMPT), `src/grill/seed.py` (PROMPT)
- Test: `tests/test_triage.py`, `tests/test_seed.py` (one prompt-content assertion each, matching how those files already test prompt text)

- [ ] **Step 1: Write the failing tests.** In `tests/test_triage.py` (place alongside existing prompt tests, matching local style):

```python
def test_the_prompt_prefers_technical_mechanisms():
    """Prompt-only steering from the 2026-07-22 design: prefer moments whose
    core is a mechanism, down-rank behavioural or process moments."""
    from grill.triage import PROMPT

    assert "technical mechanism" in PROMPT
    assert "Down-rank" in PROMPT
```

In `tests/test_seed.py`:

```python
def test_the_prompt_prefers_technical_mechanisms():
    from grill.seed import PROMPT

    assert "technical mechanism" in PROMPT
```

- [ ] **Step 2: Run to verify failure:** `uv run pytest tests/test_triage.py tests/test_seed.py -q` — the two new tests FAIL.

- [ ] **Step 3: Implement.** In `src/grill/triage.py`, insert between the "What qualifies" signal list and "What does not qualify":

```
## What to prefer

Prefer moments whose core is a technical mechanism: API semantics, tool
behavior, data formats, configuration effects, algorithms. Down-rank
behavioural or process moments — why a message was phrased a certain way,
workflow or etiquette choices, why the assistant took the approach it took.
A mechanism has a right answer a question can test; a process choice mostly
does not.
```

In `src/grill/seed.py`, append to the `hypothesis` bullet in "What to produce":

```
  Aim the hypothesis at the technical mechanism at the moment's core — API
  semantics, tool behavior, data formats, configuration effects, algorithms —
  rather than at a behavioural or process reading of the same moment. A claim
  about a mechanism can be settled by an answer; a claim about process cannot.
```

- [ ] **Step 4: Run the whole suite:** `uv run pytest -q` — expected PASS.

- [ ] **Step 5: Commit:** `git add -A && git commit -m "feat: triage and seed prompts prefer technical mechanisms"`

---

### Task 6: Backfill — mint options for legacy probes

**Files:**
- Create: `src/grill/backfill.py`
- Modify: `src/grill/storage.py` (`legacy_probes`, `set_choices`)
- Test: `tests/test_backfill.py` (new), `tests/test_storage.py`

**Interfaces:**
- Consumes: `validate_choices`, `shuffle_choices`, `ProbeRejected`, `CORRECTION`, `MAX_ATTEMPTS` from `grill.probe`; `complete`, `LLMError`, `extract_json_object` from `grill.llm`.
- Produces: `Store.legacy_probes() -> list[sqlite3.Row]` (columns `id, question, criteria, topic, hypothesis`; unasked, unexpired, `options IS NULL`); `Store.set_choices(probe_id: int, options: tuple[str, ...], correct_idx: int, explanation: str) -> None`; `mint(question, criteria, topic, hypothesis, *, complete=..., attempts=MAX_ATTEMPTS, rng=None) -> tuple[tuple[str, ...], int, str]`; `main() -> int` runnable as `python -m grill.backfill`.

- [ ] **Step 1: Write the failing storage tests** in `tests/test_storage.py`, inside `TestMigration` (it owns the legacy fixture):

```python
    def test_legacy_probes_lists_the_unasked_unexpired_legacy_row(self, tmp_path: Path):
        with Store(self.legacy_db(tmp_path)) as store:
            rows = store.legacy_probes()
        assert [r["id"] for r in rows] == [1]
        assert rows[0]["topic"] == "issue linking"
        assert "cross-reference" in rows[0]["criteria"]

    def test_set_choices_makes_a_legacy_probe_servable(self, tmp_path: Path):
        with Store(self.legacy_db(tmp_path)) as store:
            store.set_choices(1, ("right", "wrong", "wronger"), 0, "because.")
            pending = store.next_probe()
            assert pending is not None
            assert pending.options == ("right", "wrong", "wronger")
            assert pending.correct_idx == 0
            assert store.legacy_probes() == []
```

- [ ] **Step 2: Run to verify failure:** `uv run pytest tests/test_storage.py -q` — new tests FAIL (`legacy_probes` undefined).

- [ ] **Step 3: Implement the storage half** in `src/grill/storage.py`:

```python
    def legacy_probes(self) -> list[sqlite3.Row]:
        """Unasked, unexpired probes from before multiple choice.

        The backfill's worklist. Expired and already-asked rows are excluded for
        the same reason `next_probe` excludes them: nothing will ever serve
        them, so minting options for them is money spent on a row nobody reads.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PROBE_TTL_DAYS)).isoformat()
        return self.conn.execute(
            "SELECT p.id, p.question, p.criteria, s.topic, s.hypothesis"
            " FROM probes p"
            " JOIN seeds s ON s.id = p.seed_id"
            " LEFT JOIN asks a ON a.probe_id = p.id"
            " WHERE a.id IS NULL AND p.created_at >= ? AND p.options IS NULL"
            " ORDER BY p.id",
            (cutoff,),
        ).fetchall()

    def set_choices(
        self,
        probe_id: int,
        options: tuple[str, ...],
        correct_idx: int,
        explanation: str,
    ) -> None:
        """Attach minted choices to a legacy probe, making it servable."""
        self.conn.execute(
            "UPDATE probes SET options = ?, correct_idx = ?, explanation = ?"
            " WHERE id = ?",
            (json.dumps(list(options)), correct_idx, explanation, probe_id),
        )
        self.conn.commit()
```

Run: `uv run pytest tests/test_storage.py -q` — PASS.

- [ ] **Step 4: Write the failing backfill tests** in `tests/test_backfill.py`:

```python
"""Tests for the one-off legacy backfill.

The LLM is injected; nothing here spends. What these pin down: minted options
pass the same structural gates as generated ones, the shuffle property holds,
and a probe whose call keeps failing is left legacy rather than half-written.
"""

from __future__ import annotations

import json
import random

import pytest

from grill.backfill import mint
from grill.llm import Completion, LLMError
from grill.probe import MAX_ATTEMPTS


def completion(text: str) -> Completion:
    return Completion(text=text, cost_usd=0.01, duration_ms=500)


def response(**overrides) -> str:
    body = {
        "options": [
            "A closing keyword that closes the issue on merge",
            "A cross-reference event on the issue's timeline",
            "A label linking the PR to the milestone",
        ],
        "correct": 1,
        "explanation": "GitHub only autolinks `#N` after a non-word character.",
    }
    body.update(overrides)
    return json.dumps(body)


ARGS = dict(
    question="What did the swap cause GitHub to create?",
    criteria=["names the cross-reference event"],
    topic="issue linking",
    hypothesis="accepted autolinking without knowing the non-word rule",
)


def test_mints_shuffled_choices_with_a_true_correct_idx():
    for seed_value in range(30):
        options, correct_idx, explanation = mint(
            **ARGS,
            complete=lambda prompt: completion(response()),
            rng=random.Random(seed_value),
        )
        assert options[correct_idx] == "A cross-reference event on the issue's timeline"
        assert explanation.startswith("GitHub only autolinks")


def test_the_prompt_carries_the_stored_question_and_criteria():
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return completion(response())

    mint(**ARGS, complete=fake)

    assert ARGS["question"] in calls[0]
    assert "names the cross-reference event" in calls[0]


def test_a_rejected_response_is_retried_with_the_correction():
    calls = []

    def fake(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return completion(response(options=["only", "two"], correct=0))
        return completion(response())

    options, correct_idx, _ = mint(**ARGS, complete=fake)

    assert len(calls) == 2
    assert "rejected" in calls[1]


def test_gives_up_after_the_attempt_budget():
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return completion(response(correct=9))

    with pytest.raises(LLMError):
        mint(**ARGS, complete=fake)

    assert len(calls) == MAX_ATTEMPTS
```

- [ ] **Step 5: Run to verify failure:** `uv run pytest tests/test_backfill.py -q` — FAIL (no module `grill.backfill`).

- [ ] **Step 6: Create `src/grill/backfill.py`:**

```python
"""One-off: mint multiple-choice options for probes stored before the switch.

The free-text design left stored probes carrying criteria and no options, and
`next_probe` now refuses to serve them. This converts each pending one with a
single LLM call — criteria in, options/correct/explanation out — held to the
same structural gates as freshly generated probes. A probe whose call fails
stays legacy and ages out via the TTL; there is no retry loop beyond
`MAX_ATTEMPTS` and nothing here runs twice for the same row, because a
backfilled row no longer matches `options IS NULL`.
"""

from __future__ import annotations

import random
import sys

from grill.llm import Completion, LLMError, complete as _complete, extract_json_object
from grill.probe import (
    CORRECTION,
    MAX_ATTEMPTS,
    MAX_OPTIONS,
    MIN_OPTIONS,
    ProbeRejected,
    shuffle_choices,
    validate_choices,
)
from grill.storage import Store

MINT_KEYS = ("options", "correct", "explanation")

PROMPT = """\
You are converting a stored free-text question from `grill` into multiple
choice. The question already exists and must not change; you are writing the
answer options for it.

## The topic

{topic}

## What we suspect the developer does not understand

{hypothesis}

## The question they will be asked

{question}

## What a correct free-text answer had to contain

{criteria}

## What to produce

`options` — {min_options} to {max_options} one-line answers to the question,
exactly ONE of them correct. The correct option must contain the substance the
criteria above name. Every wrong option must describe a plausible wrong
MECHANISM — something a developer who half-understood would believe, including
any wrong answer the criteria were written to rule out. No "all of the above"
or "none of the above".

`correct` — the zero-based index of the correct option, as you listed them.

`explanation` — 1 to 3 sentences shown after the developer picks, right or
wrong: why the correct option is correct, in terms of the mechanism.

## Respond

One JSON object, nothing else:

{{"options": ["...", "..."], "correct": 0, "explanation": "..."}}
"""


def mint(
    question: str,
    criteria: list[str],
    topic: str,
    hypothesis: str,
    *,
    complete=_complete,
    attempts: int = MAX_ATTEMPTS,
    rng: random.Random | None = None,
) -> tuple[tuple[str, ...], int, str]:
    """Mint shuffled choices for one stored question.

    Retried on the same terms as `probe`: a structural rejection goes back with
    the correction attached, a call failure goes back unchanged.
    """
    base = PROMPT.format(
        topic=topic,
        hypothesis=hypothesis,
        question=question,
        criteria="\n".join(f"- {c}" for c in criteria) or "(none recorded)",
        min_options=MIN_OPTIONS,
        max_options=MAX_OPTIONS,
    )
    prompt = base
    last: LLMError | None = None

    for _ in range(attempts):
        try:
            completion: Completion = complete(prompt)
        except LLMError as exc:
            last = exc
            prompt = base
            continue

        try:
            parsed = extract_json_object(completion.text, salvage_keys=MINT_KEYS)
            options, correct, explanation = validate_choices(parsed, question)
        except ProbeRejected as exc:
            last = exc
            prompt = base + CORRECTION.format(question=question, reason=exc.reason)
            continue
        except LLMError as exc:
            last = exc
            prompt = base
            continue

        shuffled, correct_idx = shuffle_choices(options, correct, rng)
        return shuffled, correct_idx, explanation

    raise last if last else LLMError("backfill exhausted its attempts without an error")


def main() -> int:
    """`python -m grill.backfill` — convert every pending legacy probe, once."""
    import json

    with Store() as store:
        rows = store.legacy_probes()
        if not rows:
            print("no legacy probes to backfill.")
            return 0

        for row in rows:
            try:
                stored = json.loads(row["criteria"])
                criteria = [c for c in stored if isinstance(c, str)] if isinstance(stored, list) else []
            except (TypeError, ValueError):
                criteria = []

            try:
                options, correct_idx, explanation = mint(
                    row["question"], criteria, row["topic"], row["hypothesis"]
                )
            except LLMError as exc:
                # Left legacy on purpose: it ages out via the TTL, and a probe
                # with invented options is worse than one that expires.
                print(f"probe {row['id']}: left legacy ({exc})", file=sys.stderr)
                continue

            store.set_choices(row["id"], options, correct_idx, explanation)
            print(f"probe {row['id']}: backfilled with {len(options)} options")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run the whole suite:** `uv run pytest -q` — expected PASS.

- [ ] **Step 8: Commit:** `git add -A && git commit -m "feat: one-off backfill mints options for legacy probes"`

---

### Task 7: Run the backfill against the live database

The spec's one-off. The live DB (`~/.claude/grill/grill.db`) holds one pending free-text probe (id 1, the GH#4923 one). This spends at most `MAX_ATTEMPTS` LLM calls. Do NOT run `grill` itself — an ask consumes the probe.

- [ ] **Step 1: Look before touching:** `sqlite3 -readonly ~/.claude/grill/grill.db "SELECT id, question FROM probes WHERE options IS NULL"` — expect the single legacy probe. (Opening via `Store` also migrates the live DB; that is intended.)
- [ ] **Step 2: Back up:** `cp ~/.claude/grill/grill.db ~/.claude/grill/grill.db.pre-mc-backup`
- [ ] **Step 3: Run:** `uv run python -m grill.backfill` — expect `probe 1: backfilled with N options`. If it reports `left legacy`, that is the spec's accepted outcome: report it and stop (no retry loop beyond `MAX_ATTEMPTS`).
- [ ] **Step 4: Verify:** `sqlite3 -readonly ~/.claude/grill/grill.db "SELECT id, correct_idx, options, explanation FROM probes WHERE id = 1"` — options JSON parses, `correct_idx` in range, correct option consistent with the stored criteria (read it yourself).
- [ ] **Step 5: Nothing to commit** (live data, not repo). Note the result in the final report.

---

## Self-Review

- **Spec coverage:** stage-3 JSON shape + gates (Task 1); shuffle-at-storage (Task 1, property test); selection prompt preference (Task 5); ask flow with two prompts, mechanical verdict, ✓/✗ explanation, error-only-for-malformed (Task 3); judge/followup/criterion_results deletion, Rubric shrink (Tasks 3–4); storage columns, migration at open, `options IS NOT NULL` filter (Task 2); backfill (Tasks 6–7); turns 0-or-1 continuity (Task 3, `AnswerTurn` single). Out of scope items untouched.
- **Type consistency:** `Probe.correct_idx`/`PendingProbe.correct_idx`, `validate_choices(parsed, question)`, `shuffle_choices(options, correct, rng)`, `mint(...) -> (options, correct_idx, explanation)`, `set_choices(probe_id, options, correct_idx, explanation)` used identically across tasks.
- **Known deviation:** spec says "`criteria` disappears from the stage-3 output; `Rubric.criteria` … go with it" — done; the *column* stays (NOT NULL in the live DB, and the backfill reads it) with new rows writing `'[]'`. Rebuilding the table to drop it risks live data for no benefit.
