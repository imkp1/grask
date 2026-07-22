# Capture Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a Claude Code session ends, triage it and — if it earns a question — persist a ready-to-ask probe to SQLite, without ever blocking or crashing the session.

**Architecture:** Three units with clean seams. `storage.py` owns a SQLite `Store` with three tables. `capture.py` orchestrates the four existing stages (extract → triage → seed → probe) and never raises. `hook.py` reads the SessionEnd JSON from stdin, spawns `capture` detached, and exits 0 immediately.

**Tech Stack:** Python 3.12+, stdlib only (`sqlite3`, `json`, `subprocess`, `pathlib`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-21-capture-pipeline-design.md`

## Global Constraints

- Python `>=3.12`. Every module starts with `from __future__ import annotations`.
- **Stdlib only.** `pyproject.toml` has `dependencies = []` and this plan does not add any.
- All storage and logging lives under `~/.claude/grill/`, overridable by the `GRILL_HOME` environment variable. The db is `<home>/grill.db`, the log is `<home>/grill.log`.
- `capture_session` never raises. `hook.main` never raises and always exits 0.
- Every module gets a docstring explaining *why* it exists, matching the existing house style in `src/grill/` (see `transcript.py`, `seed.py`).
- Tests that call a real model are marked `@pytest.mark.calibration` — they are deselected by default via `addopts` in `pyproject.toml`.
- Run tests with `.venv/bin/pytest` from the repo root.

## File Structure

| File | Responsibility |
|---|---|
| Create `src/grill/storage.py` | `Store`: schema, idempotency check, three insert methods. Knows nothing about stages. |
| Create `src/grill/capture.py` | `capture_session`: the four-stage orchestration + error containment. Also the `python -m grill.capture` worker entry point. |
| Create `src/grill/hook.py` | `main`: stdin parse + detached spawn. Knows nothing about stages or storage. |
| Create `tests/test_storage.py` | Temp-db round-trips and idempotency. |
| Create `tests/test_capture.py` | Control flow with injected fakes. No model spend. |
| Create `tests/test_hook.py` | Stdin parse and spawn argv, with spawn monkeypatched. |
| Create `tests/test_capture_smoke.py` | One real end-to-end run, `calibration`-marked. |
| Modify `pyproject.toml` | Add the `grill-hook` console entry point. |
| Modify `~/.claude/settings.json` | Register the global `SessionEnd` hook. |

The dependency direction is one-way: `hook` → `capture` → `storage`. Nothing points back.

---

### Task 1: `storage.py` — the Store

**Files:**
- Create: `src/grill/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `grill.seed.Seed`, `grill.probe.Probe` (existing, unchanged).
- Produces:
  - `grill_home() -> Path`
  - `class Store`, constructed as `Store(path: Path | None = None)`, usable as a context manager
  - `Store.has_session(session_id: str) -> bool`
  - `Store.record_session(*, session_id: str, transcript_path: str, cwd: str | None, git_branch: str | None, verdict: str, signal: str | None = None, topic: str | None = None, cost_usd: float | None = None) -> None`
  - `Store.add_seed(seed: Seed) -> int` (returns the new `seeds.id`)
  - `Store.add_probe(seed_id: int, probe: Probe) -> int`
  - `Store.close() -> None`

---

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage.py`:

```python
"""Tests for the capture store.

Every test runs against a temp db. What these pin down is the part of storage
that is not SQL: a session is recorded exactly once no matter how many times
capture sees it, and a seed survives the round trip with its tuples intact.

Idempotency is tested rather than assumed because it is the only thing standing
between a re-fired hook and paying for the same session twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grill.judge import Rubric
from grill.probe import Probe
from grill.seed import Seed
from grill.storage import Store, grill_home


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "grill.db") as s:
        yield s


def a_seed(session_id: str = "0198e4f1") -> Seed:
    return Seed(
        session_id=session_id,
        turn=4,
        signal="asked_why",
        topic="idempotency of the retry path",
        quotes=("why do we need an idempotency key here?", "what happens on a replay?"),
        refs=("src/api/retry.py",),
        decision="added an idempotency key to the retry wrapper",
        hypothesis="the developer accepted the key without knowing what it dedupes against",
        cost_usd=0.21,
    )


def a_probe() -> Probe:
    return Probe(
        question="What would happen if two retries carried the same idempotency key?",
        rubric=Rubric(
            topic="idempotency of the retry path",
            hypothesis="the developer accepted the key without knowing what it dedupes against",
            criteria=("names the dedupe window", "says the second call is a no-op"),
        ),
        cost_usd=0.13,
    )


def test_grill_home_honours_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRILL_HOME", str(tmp_path / "elsewhere"))
    assert grill_home() == tmp_path / "elsewhere"


def test_records_a_silent_session(store: Store):
    store.record_session(
        session_id="0198e4f1",
        transcript_path="/tmp/0198e4f1.jsonl",
        cwd="/repo",
        git_branch="main",
        verdict="silent",
        cost_usd=0.04,
    )
    rows = store.conn.execute("SELECT verdict, cost_usd FROM sessions").fetchall()
    assert [tuple(r) for r in rows] == [("silent", 0.04)]


def test_double_capture_is_a_no_op(store: Store):
    for _ in range(2):
        store.record_session(
            session_id="0198e4f1",
            transcript_path="/tmp/0198e4f1.jsonl",
            cwd=None,
            git_branch=None,
            verdict="silent",
        )
    assert store.conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_has_session_is_true_after_recording(store: Store):
    assert not store.has_session("0198e4f1")
    store.record_session(
        session_id="0198e4f1",
        transcript_path="/tmp/0198e4f1.jsonl",
        cwd=None,
        git_branch=None,
        verdict="error",
    )
    assert store.has_session("0198e4f1")


def test_seed_round_trips_with_tuples_intact(store: Store):
    store.record_session(
        session_id="0198e4f1",
        transcript_path="/tmp/0198e4f1.jsonl",
        cwd=None,
        git_branch=None,
        verdict="ask",
    )
    seed_id = store.add_seed(a_seed())
    row = store.conn.execute(
        "SELECT session_id, turn, signal, topic, quotes, refs, decision, hypothesis, cost_usd"
        " FROM seeds WHERE id = ?",
        (seed_id,),
    ).fetchone()
    import json

    assert row["session_id"] == "0198e4f1"
    assert row["turn"] == 4
    assert json.loads(row["quotes"]) == [
        "why do we need an idempotency key here?",
        "what happens on a replay?",
    ]
    assert json.loads(row["refs"]) == ["src/api/retry.py"]
    assert row["hypothesis"].startswith("the developer accepted")
    assert row["cost_usd"] == 0.21


def test_probe_stores_criteria_only(store: Store):
    store.record_session(
        session_id="0198e4f1",
        transcript_path="/tmp/0198e4f1.jsonl",
        cwd=None,
        git_branch=None,
        verdict="ask",
    )
    seed_id = store.add_seed(a_seed())
    probe_id = store.add_probe(seed_id, a_probe())
    row = store.conn.execute(
        "SELECT seed_id, question, criteria FROM probes WHERE id = ?", (probe_id,)
    ).fetchone()
    import json

    assert row["seed_id"] == seed_id
    assert row["question"].startswith("What would happen")
    assert json.loads(row["criteria"]) == [
        "names the dedupe window",
        "says the second call is a no-op",
    ]


def test_a_seed_needs_a_session(store: Store):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.add_seed(a_seed(session_id="never-recorded"))


def test_schema_is_applied_twice_without_error(tmp_path: Path):
    path = tmp_path / "grill.db"
    with Store(path) as first:
        first.record_session(
            session_id="0198e4f1",
            transcript_path="/tmp/0198e4f1.jsonl",
            cwd=None,
            git_branch=None,
            verdict="silent",
        )
    with Store(path) as second:
        assert second.has_session("0198e4f1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_storage.py -v`
Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'grill.storage'`.

- [ ] **Step 3: Write the implementation**

Create `src/grill/storage.py`:

```python
"""Where capture puts what it found.

One SQLite file under `~/.claude/grill/`. Three tables, one per stage that
produces something worth keeping.

Silence and failure are recorded, not just keeps. That is deliberate: keep-rate
and failure-rate are the two numbers that say whether any of this is working, and
a table you have to remember to populate is a table that lies. It is also what
makes capture idempotent — a session_id already present means we have seen it,
whatever we concluded.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from grill.probe import Probe
from grill.seed import Seed

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    transcript_path TEXT NOT NULL,
    cwd             TEXT,
    git_branch      TEXT,
    verdict         TEXT NOT NULL,
    signal          TEXT,
    topic           TEXT,
    cost_usd        REAL,
    triaged_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seeds (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    turn        INTEGER NOT NULL,
    signal      TEXT NOT NULL,
    topic       TEXT NOT NULL,
    quotes      TEXT NOT NULL,
    refs        TEXT NOT NULL,
    decision    TEXT NOT NULL,
    hypothesis  TEXT NOT NULL,
    cost_usd    REAL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probes (
    id          INTEGER PRIMARY KEY,
    seed_id     INTEGER NOT NULL REFERENCES seeds(id),
    question    TEXT NOT NULL,
    criteria    TEXT NOT NULL,
    cost_usd    REAL,
    created_at  TEXT NOT NULL
);
"""


def grill_home() -> Path:
    """Where the db and the log live. Env-overridable so tests never touch the real one."""
    override = os.environ.get("GRILL_HOME")
    return Path(override) if override else Path.home() / ".claude" / "grill"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """The capture database.

    Opens and migrates on construction; `CREATE TABLE IF NOT EXISTS` means that
    is safe to do on every hook firing, which is exactly how often it happens.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or grill_home() / "grill.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # Off by default in sqlite, and the one thing keeping an orphaned seed
        # from outliving the session that explains it.
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def has_session(self, session_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None

    def record_session(
        self,
        *,
        session_id: str,
        transcript_path: str,
        cwd: str | None,
        git_branch: str | None,
        verdict: str,
        signal: str | None = None,
        topic: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Record one triaged session. A second call for the same id does nothing.

        `cost_usd` is triage's cost alone. Seed and probe carry their own, because
        a single column that means different things per verdict is a column nobody
        can sum.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions"
            " (session_id, transcript_path, cwd, git_branch, verdict, signal, topic,"
            "  cost_usd, triaged_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                transcript_path,
                cwd,
                git_branch,
                verdict,
                signal,
                topic,
                cost_usd,
                _now(),
            ),
        )
        self.conn.commit()

    def add_seed(self, seed: Seed) -> int:
        cursor = self.conn.execute(
            "INSERT INTO seeds"
            " (session_id, turn, signal, topic, quotes, refs, decision, hypothesis,"
            "  cost_usd, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seed.session_id,
                seed.turn,
                seed.signal,
                seed.topic,
                json.dumps(list(seed.quotes)),
                json.dumps(list(seed.refs)),
                seed.decision,
                seed.hypothesis,
                seed.cost_usd,
                _now(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def add_probe(self, seed_id: int, probe: Probe) -> int:
        """Store the question and its criteria.

        Not the whole rubric: `topic` and `hypothesis` are already on the seed, and
        the rubric is reassembled from both when something needs to grade an answer.
        """
        cursor = self.conn.execute(
            "INSERT INTO probes (seed_id, question, criteria, cost_usd, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                seed_id,
                probe.question,
                json.dumps(list(probe.rubric.criteria)),
                probe.cost_usd,
                _now(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_storage.py -v`
Expected: PASS, 9 passed.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/grill/storage.py tests/test_storage.py
git commit -m "feat: the capture store — sessions, seeds, probes, idempotent by session_id"
```

---

### Task 2: `capture.py` — the orchestration

**Files:**
- Create: `src/grill/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `Store` from Task 1 (`has_session`, `record_session`, `add_seed`, `add_probe`, `grill_home`); existing `grill.transcript.extract`, `grill.dialogue.extract_dialogue`, `grill.triage.triage`, `grill.select.select`, `grill.seed.seed`, `grill.probe.probe`, `grill.llm.LLMError`.
- Produces:
  - `capture_session(transcript_path: Path, store: Store, *, triage=triage, seed=seed, probe=probe, extract_dialogue=extract_dialogue) -> None`
  - `main(argv: list[str] | None = None) -> int` — the `python -m grill.capture <transcript_path>` worker

**Two things worth knowing before writing this:**

1. `triage()` never raises. On failure it returns a `TriageVerdict` with `verdict="silent"` and `.error` set. Capture must map *that* to an `error` row, not a `silent` one — otherwise a broken model call is indistinguishable from a boring session and the failure-rate number is a lie.
2. `TriageVerdict` does not carry the selected `Moment` object, only its fields. `seed()` needs the `Moment`. Recover it by calling `select(verdict.moments)` — `select` is pure and deterministic, so it returns the same moment triage chose.

---

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capture.py`:

```python
"""Tests for the capture orchestration.

No model is called here. Every stage is injected, so what these pin down is the
control flow: who gets recorded, what gets skipped, and — the one that matters —
that a stage blowing up produces an error row and a log line rather than an
exception. Capture runs detached with nothing watching its exit code, so an
exception it lets escape is a failure nobody ever learns about.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grill.capture import capture_session
from grill.judge import Rubric
from grill.llm import LLMError
from grill.probe import Probe
from grill.seed import Seed
from grill.storage import Store
from grill.triage import Moment, TriageVerdict


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "grill.db") as s:
        yield s


def transcript(tmp_path: Path, *texts: str) -> Path:
    """A minimal real transcript: stage 0 reads these lines for real."""
    path = tmp_path / "0198e4f1.jsonl"
    lines = []
    for text in texts:
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "promptSource": "typed",
                    "cwd": "/repo",
                    "gitBranch": "main",
                    "timestamp": "2026-07-21T08:00:00Z",
                    "message": {"content": text},
                }
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def a_moment() -> Moment:
    return Moment(
        turn=0,
        signal="asked_why",
        topic="idempotency of the retry path",
        quote="why do we need an idempotency key here?",
        shows="asked why rather than accepting the retry wrapper",
    )


def ask_verdict() -> TriageVerdict:
    moment = a_moment()
    return TriageVerdict(
        session_id="0198e4f1",
        verdict="ask",
        signal=moment.signal,
        topic=moment.topic,
        quote=moment.quote,
        reason=moment.shows,
        cost_usd=0.05,
        moments=[moment],
        candidates=1,
    )


def a_seed() -> Seed:
    return Seed(
        session_id="0198e4f1",
        turn=0,
        signal="asked_why",
        topic="idempotency of the retry path",
        quotes=("why do we need an idempotency key here?",),
        refs=("src/api/retry.py",),
        decision="added an idempotency key to the retry wrapper",
        hypothesis="the developer accepted the key without knowing what it dedupes against",
        cost_usd=0.21,
    )


def a_probe() -> Probe:
    return Probe(
        question="What would happen if two retries carried the same idempotency key?",
        rubric=Rubric(
            topic="idempotency of the retry path",
            hypothesis="the developer accepted the key without knowing what it dedupes against",
            criteria=("names the dedupe window",),
        ),
        cost_usd=0.13,
    )


def counts(store: Store) -> tuple[int, int, int]:
    return tuple(
        store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("sessions", "seeds", "probes")
    )


def test_no_human_turns_records_silent_without_triaging(store: Store, tmp_path: Path):
    # A `raise` here would be swallowed by capture's own error containment and the
    # test would pass for the wrong reason. Record the call and assert on it after.
    called = []

    capture_session(transcript(tmp_path), store, triage=lambda session: called.append(session))

    assert called == [], "triage must not run on a session with no human turns"
    assert counts(store) == (1, 0, 0)
    row = store.conn.execute("SELECT verdict FROM sessions").fetchone()
    assert row["verdict"] == "silent"


def test_silent_verdict_stores_only_a_session_row(store: Store, tmp_path: Path):
    def silent(session):
        return TriageVerdict(
            session_id=session.session_id, verdict="silent", reason="nothing here", cost_usd=0.03
        )

    capture_session(transcript(tmp_path, "ship it"), store, triage=silent)

    assert counts(store) == (1, 0, 0)
    row = store.conn.execute("SELECT verdict, cwd, git_branch, cost_usd FROM sessions").fetchone()
    assert row["verdict"] == "silent"
    assert row["cwd"] == "/repo"
    assert row["git_branch"] == "main"
    assert row["cost_usd"] == 0.03


def test_ask_verdict_stores_session_seed_and_probe(store: Store, tmp_path: Path):
    capture_session(
        transcript(tmp_path, "why do we need an idempotency key here?"),
        store,
        triage=lambda session: ask_verdict(),
        seed=lambda dialogue, moment: a_seed(),
        probe=lambda seed, dialogue: a_probe(),
    )

    assert counts(store) == (1, 1, 1)
    row = store.conn.execute("SELECT verdict, signal, topic FROM sessions").fetchone()
    assert row["verdict"] == "ask"
    assert row["signal"] == "asked_why"
    assert row["topic"] == "idempotency of the retry path"


def test_seed_receives_the_moment_triage_selected(store: Store, tmp_path: Path):
    seen = {}

    def spy_seed(dialogue, moment):
        seen["turn"] = moment.turn
        seen["topic"] = moment.topic
        return a_seed()

    capture_session(
        transcript(tmp_path, "why do we need an idempotency key here?"),
        store,
        triage=lambda session: ask_verdict(),
        seed=spy_seed,
        probe=lambda seed, dialogue: a_probe(),
    )

    assert seen == {"turn": 0, "topic": "idempotency of the retry path"}


def test_seed_failure_is_recorded_as_error_not_raised(store: Store, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRILL_HOME", str(tmp_path))

    def boom(dialogue, moment):
        raise LLMError("model call failed")

    capture_session(
        transcript(tmp_path, "why do we need an idempotency key here?"),
        store,
        triage=lambda session: ask_verdict(),
        seed=boom,
    )

    assert counts(store) == (1, 0, 0)
    assert store.conn.execute("SELECT verdict FROM sessions").fetchone()["verdict"] == "error"
    assert "model call failed" in (tmp_path / "grill.log").read_text(encoding="utf-8")


def test_unexpected_exception_is_also_contained(store: Store, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRILL_HOME", str(tmp_path))

    def boom(session):
        raise ValueError("something nobody predicted")

    capture_session(transcript(tmp_path, "ship it"), store, triage=boom)

    assert store.conn.execute("SELECT verdict FROM sessions").fetchone()["verdict"] == "error"
    assert "something nobody predicted" in (tmp_path / "grill.log").read_text(encoding="utf-8")


def test_triage_internal_failure_is_an_error_not_silence(store: Store, tmp_path: Path):
    def failed(session):
        return TriageVerdict(
            session_id=session.session_id,
            verdict="silent",
            reason="triage failed: model call failed",
            error="timeout after 300s",
        )

    capture_session(transcript(tmp_path, "ship it"), store, triage=failed)

    assert store.conn.execute("SELECT verdict FROM sessions").fetchone()["verdict"] == "error"


def test_already_captured_session_is_skipped(store: Store, tmp_path: Path):
    path = transcript(tmp_path, "ship it")
    store.record_session(
        session_id="0198e4f1",
        transcript_path=str(path),
        cwd=None,
        git_branch=None,
        verdict="silent",
    )

    called = []

    capture_session(path, store, triage=lambda session: called.append(session))

    assert called == [], "an already-captured session must not be triaged again"
    assert counts(store) == (1, 0, 0)


def test_a_missing_transcript_does_not_raise(store: Store, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRILL_HOME", str(tmp_path))
    capture_session(tmp_path / "gone.jsonl", store)
    assert (tmp_path / "grill.log").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_capture.py -v`
Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'grill.capture'`.

- [ ] **Step 3: Write the implementation**

Create `src/grill/capture.py`:

```python
"""The whole pipeline, end to end, for one session.

Runs detached from a SessionEnd hook with nothing watching its exit code. That
single fact decides the error handling: an exception here reaches no one, so
every failure has to become a row and a log line instead. `capture_session` does
not raise. If it ever does, a session ends and grill silently forgets it.

Order is extract → triage → seed → probe, cheapest first. Stage 0 is free and
filters sessions with no human in them; triage is one call and filters the
majority; only what survives both pays for stages 2 and 3.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from grill.dialogue import extract_dialogue as _extract_dialogue
from grill.probe import probe as _probe
from grill.seed import seed as _seed
from grill.select import select
from grill.storage import Store, grill_home
from grill.transcript import extract
from grill.triage import triage as _triage


def log(message: str) -> None:
    """Append to the capture log. Never raises — this is the failure path itself."""
    try:
        path = grill_home() / "grill.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass


def capture_session(
    transcript_path: Path,
    store: Store,
    *,
    triage=_triage,
    seed=_seed,
    probe=_probe,
    extract_dialogue=_extract_dialogue,
) -> None:
    """Triage one ended session and persist whatever it earned.

    The stages are injectable so the control flow can be tested without spending
    money on a model. Defaults are the real thing.
    """
    session_id = Path(transcript_path).stem
    try:
        if store.has_session(session_id):
            return

        session = extract(Path(transcript_path))

        if not session.turns:
            # Stage 0's floor. No human said anything, so there is nothing to ask
            # about and no reason to pay triage to confirm it.
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict="silent",
            )
            return

        verdict = triage(session)

        # triage() never raises; it reports failure by returning silent with
        # `.error` set. Recording that as silence would make a broken model call
        # look like a boring session, and the failure-rate number would be a lie.
        if verdict.error:
            log(f"{session_id} triage error: {verdict.error}")
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict="error",
                cost_usd=verdict.cost_usd,
            )
            return

        if not verdict.kept:
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict="silent",
                cost_usd=verdict.cost_usd,
            )
            return

        # The verdict carries the selected moment's fields but not the Moment
        # itself, and stage 2 needs the object. `select` is pure, so re-running it
        # over the same moments returns the same one triage chose.
        moment = select(verdict.moments)
        if moment is None:
            raise RuntimeError("verdict is 'ask' but no moment survives selection")

        dialogue = extract_dialogue(Path(transcript_path))
        the_seed = seed(dialogue, moment)
        the_probe = probe(the_seed, dialogue)

        store.record_session(
            session_id=session.session_id,
            transcript_path=str(transcript_path),
            cwd=session.cwd,
            git_branch=session.git_branch,
            verdict="ask",
            signal=verdict.signal,
            topic=verdict.topic,
            cost_usd=verdict.cost_usd,
        )
        seed_id = store.add_seed(the_seed)
        store.add_probe(seed_id, the_probe)

    except Exception:
        log(f"{session_id} capture failed:\n{traceback.format_exc()}")
        try:
            store.record_session(
                session_id=session_id,
                transcript_path=str(transcript_path),
                cwd=None,
                git_branch=None,
                verdict="error",
            )
        except Exception:
            log(f"{session_id} could not even record the error:\n{traceback.format_exc()}")


def main(argv: list[str] | None = None) -> int:
    """The detached worker: `python -m grill.capture <transcript_path>`."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        log("worker started with no transcript path")
        return 0
    try:
        with Store() as store:
            capture_session(Path(args[0]), store)
    except Exception:
        log(f"worker failed before capture:\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_capture.py -v`
Expected: PASS, 9 passed.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/grill/capture.py tests/test_capture.py
git commit -m "feat: capture — the four stages end to end, failures become rows not exceptions"
```

---

### Task 3: `hook.py` — the entry point

**Files:**
- Create: `src/grill/hook.py`
- Test: `tests/test_hook.py`

**Interfaces:**
- Consumes: `grill.capture.log` (Task 2).
- Produces:
  - `spawn(transcript_path: str) -> None` — starts the detached worker
  - `main(stdin=sys.stdin, spawn=spawn) -> int` — always returns 0

The SessionEnd payload Claude Code writes to stdin looks like:

```json
{"session_id": "0198e4f1-...", "transcript_path": "/Users/x/.claude/projects/-repo/0198e4f1-....jsonl", "cwd": "/repo", "hook_event_name": "SessionEnd", "reason": "clear"}
```

---

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hook.py`:

```python
"""Tests for the SessionEnd hook.

The worker is never actually spawned here; `spawn` is injected. What these pin
down is that the hook is quiet and fast: it parses stdin, hands off one path, and
returns 0 no matter what it was given. A hook that errors on the way out of a
session is worse than one that does nothing, because the developer sees it and
has no idea what it was for.
"""

from __future__ import annotations

import io
import json

from grill.hook import main


def stdin(payload: object) -> io.StringIO:
    return io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))


def test_spawns_the_worker_with_the_transcript_path():
    spawned = []
    code = main(
        stdin=stdin(
            {
                "session_id": "0198e4f1",
                "transcript_path": "/p/0198e4f1.jsonl",
                "hook_event_name": "SessionEnd",
            }
        ),
        spawn=spawned.append,
    )
    assert code == 0
    assert spawned == ["/p/0198e4f1.jsonl"]


def test_malformed_stdin_exits_zero_without_spawning():
    spawned = []
    assert main(stdin=stdin("not json at all"), spawn=spawned.append) == 0
    assert spawned == []


def test_empty_stdin_exits_zero_without_spawning():
    spawned = []
    assert main(stdin=io.StringIO(""), spawn=spawned.append) == 0
    assert spawned == []


def test_missing_transcript_path_exits_zero_without_spawning():
    spawned = []
    assert main(stdin=stdin({"session_id": "0198e4f1"}), spawn=spawned.append) == 0
    assert spawned == []


def test_a_failing_spawn_still_exits_zero():
    def boom(path):
        raise OSError("fork failed")

    assert main(stdin=stdin({"transcript_path": "/p/0198e4f1.jsonl"}), spawn=boom) == 0


def test_spawn_argv_is_the_running_interpreter_and_the_capture_module(monkeypatch, tmp_path):
    import subprocess
    import sys

    from grill import hook

    # spawn() opens the real log file, so redirect GRILL_HOME or this test
    # scribbles in the developer's actual ~/.claude/grill/.
    monkeypatch.setenv("GRILL_HOME", str(tmp_path))
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    hook.spawn("/p/0198e4f1.jsonl")

    assert seen["argv"] == [sys.executable, "-m", "grill.capture", "/p/0198e4f1.jsonl"]
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_hook.py -v`
Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'grill.hook'`.

- [ ] **Step 3: Write the implementation**

Create `src/grill/hook.py`:

```python
"""The SessionEnd entry point.

Reads the hook payload from stdin, starts the capture worker detached, and
returns immediately. The parent is gone long before the first model call, so
whether the harness honours an async hook does not matter — this is non-blocking
by construction rather than by permission.

Everything is swallowed. The developer is on their way out of a session; a hook
that raises at that moment produces a scary message about a tool they cannot see
and did not ask about.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Callable, TextIO

from grill.capture import log
from grill.storage import grill_home


def spawn(transcript_path: str) -> None:
    """Start the capture worker and forget about it.

    `start_new_session` (setsid) detaches it from this process group, so it
    survives the session ending. stdin is closed and both output streams go to
    the log — a detached process writing to an inherited terminal is a process
    that scribbles on the next thing the developer does.
    """
    path = grill_home() / "grill.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-m", "grill.capture", transcript_path],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=handle,
    )


def main(stdin: TextIO | None = None, spawn: Callable[[str], None] = spawn) -> int:
    """Parse the SessionEnd payload and hand off. Always returns 0."""
    stream = sys.stdin if stdin is None else stdin
    try:
        payload = json.loads(stream.read() or "{}")
        transcript_path = payload.get("transcript_path") if isinstance(payload, dict) else None
        if isinstance(transcript_path, str) and transcript_path:
            spawn(transcript_path)
    except Exception as exc:
        log(f"hook ignored a payload it could not use: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_hook.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/grill/hook.py tests/test_hook.py
git commit -m "feat: the SessionEnd hook — parse stdin, detach the worker, exit 0"
```

---

### Task 4: Wire it up and prove it end to end

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_capture_smoke.py`
- Modify: `~/.claude/settings.json`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: a `grill-hook` executable at `.venv/bin/grill-hook`, and a registered global SessionEnd hook.

This task is one unit because the smoke test is what proves the wiring: an entry point nobody has run is a claim, not a deliverable.

---

- [ ] **Step 1: Add the console entry point**

In `pyproject.toml`, immediately after the `[project]` block's `dependencies = []` line, add:

```toml
[project.scripts]
grill-hook = "grill.hook:main"
```

- [ ] **Step 2: Install editable and verify the executable exists**

Run:

```bash
uv pip install -e . && ls -l .venv/bin/grill-hook
```

Expected: the file exists and is executable.

- [ ] **Step 3: Verify the installed hook is quiet on a payload it cannot use**

Run:

```bash
echo '{"hook_event_name":"SessionEnd"}' | .venv/bin/grill-hook; echo "exit=$?"
```

Expected: no output, `exit=0`.

- [ ] **Step 4: Write the end-to-end smoke test**

This one calls the real model and costs roughly $0.59. That number is the measurement, not an accident — it is what one captured session costs, and the reason it is written down.

Create `tests/test_capture_smoke.py`:

```python
"""One real run of the whole pipeline, against a real transcript.

Costs money — roughly $0.59 for a session that triages to `ask`. Marked
`calibration` so the ordinary suite stays free and offline; run it deliberately:

    .venv/bin/pytest -m calibration tests/test_capture_smoke.py -s

Every other test in this repo injects the stages. This is the only one that
proves the four of them actually compose, which is a different claim from each
of them working alone.

Point it at a transcript you already know triages to `ask`, via GRILL_SMOKE_TRANSCRIPT.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from grill.capture import capture_session
from grill.storage import Store
from grill.transcript import find_transcripts


def a_keep_transcript() -> Path:
    override = os.environ.get("GRILL_SMOKE_TRANSCRIPT")
    if override:
        return Path(override)
    found = find_transcripts()
    if not found:
        pytest.skip("no transcripts on disk to smoke-test against")
    return found[0]


@pytest.mark.calibration
def test_one_real_session_end_to_end(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("GRILL_HOME", str(tmp_path))
    path = a_keep_transcript()

    with Store(tmp_path / "grill.db") as store:
        capture_session(path, store)

        session = store.conn.execute(
            "SELECT session_id, verdict, signal, topic, cost_usd FROM sessions"
        ).fetchone()
        assert session is not None, "capture recorded nothing at all"
        assert session["verdict"] in {"ask", "silent"}, "capture errored — see grill.log"

        with capsys.disabled():
            print(f"\ntranscript: {path}")
            print(f"verdict:    {session['verdict']}  ({session['topic']})")

        if session["verdict"] != "ask":
            pytest.skip(f"transcript triaged {session['verdict']}; nothing further to check")

        seed = store.conn.execute("SELECT * FROM seeds").fetchone()
        probe = store.conn.execute("SELECT * FROM probes").fetchone()
        assert seed is not None and probe is not None

        assert seed["hypothesis"].strip()
        assert json.loads(seed["quotes"]), "a stored seed with no quotes broke the evidence rule"
        assert probe["question"].strip().endswith("?")
        assert json.loads(probe["criteria"]), "a probe with no criteria cannot be graded"
        assert probe["seed_id"] == seed["id"]

        total = sum(
            row[0] or 0
            for row in [
                (session["cost_usd"],),
                (seed["cost_usd"],),
                (probe["cost_usd"],),
            ]
        )
        with capsys.disabled():
            print(f"question:   {probe['question']}")
            print(f"cost:       ${total:.2f}")
```

- [ ] **Step 5: Confirm the smoke test is deselected by default**

Run: `.venv/bin/pytest -q`
Expected: PASS. The output line reads `... deselected` and no model call happens.

- [ ] **Step 6: Run the smoke test for real**

Run:

```bash
.venv/bin/pytest -m calibration tests/test_capture_smoke.py -s
```

Expected: PASS, and the printed verdict, question, and cost. Record the actual cost — if it is far from $0.59 that is a finding worth writing down, not a test to adjust.

- [ ] **Step 7: Commit the code**

```bash
git add pyproject.toml tests/test_capture_smoke.py
git commit -m "feat: grill-hook entry point, and one real end-to-end run to prove it composes"
```

- [ ] **Step 8: Register the global SessionEnd hook**

Read `~/.claude/settings.json` first — it already has content, and this must merge into the existing `hooks` object rather than replace it. Add a `SessionEnd` entry:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/grill/.venv/bin/grill-hook"
          }
        ]
      }
    ]
  }
}
```

The absolute path is required: the hook fires from whatever cwd the session was in, which is almost never this repo.

If `SessionEnd` already exists in that file, append this `{"hooks": [...]}` block to the existing array instead of overwriting it.

- [ ] **Step 9: Verify the registration**

Run:

```bash
.venv/bin/python -c "import json,pathlib;print(json.dumps(json.loads(pathlib.Path.home().joinpath('.claude/settings.json').read_text())['hooks']['SessionEnd'],indent=2))"
```

Expected: the block above, with the absolute path intact.

Then end a real Claude Code session and check the capture landed:

```bash
.venv/bin/python -c "import sqlite3,pathlib;db=pathlib.Path.home()/'.claude/grill/grill.db';print([tuple(r) for r in sqlite3.connect(db).execute('SELECT session_id, verdict, topic FROM sessions ORDER BY triaged_at DESC LIMIT 5')])"
tail -20 ~/.claude/grill/grill.log
```

Expected: a row for the session that just ended. If the db does not exist, the log is where the reason is.

---

## Done when

- `.venv/bin/pytest` is green and the calibration test is deselected.
- `.venv/bin/pytest -m calibration` has been run once, and its cost recorded.
- Ending a real session produces a row in `~/.claude/grill/grill.db`.
