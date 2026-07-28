"""Tests for the capture store.

Every test runs against a temp db. What these pin down is the part of storage
that is not SQL: a session is recorded exactly once no matter how many times
capture sees it, and a seed survives the round trip with its tuples intact.

Idempotency is tested rather than assumed because it is the only thing standing
between a re-fired hook and paying for the same session twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grask.ask import AnswerTurn, Interrogation
from grask.probe import Probe, Rubric
from grask.seed import Seed
from grask.storage import CAPTURE_STALE_MINUTES, PROBE_TTL_DAYS, Store, grask_home


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def an_interrogation(*, probe_id: int, turn_count: int = 1) -> Interrogation:
    turns = tuple(
        AnswerTurn(
            turn=n,
            question=a_probe().question if n == 0 else f"follow-up {n}",
            answer=f"answer {n}",
        )
        for n in range(turn_count)
    )
    return Interrogation(
        probe_id=probe_id,
        outcome="passed",
        confidence=95,
        objection=None,
        turns=turns,
        cost_usd=0.0,
    )


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "grask.db") as s:
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
        duration_ms=900,
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
        rubric=Rubric(
            topic="idempotency of the retry path",
            hypothesis="the developer accepted the key without knowing what it dedupes against",
        ),
        cost_usd=0.13,
        duration_ms=1300,
    )


def test_grask_home_honours_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRASK_HOME", str(tmp_path / "elsewhere"))
    assert grask_home() == tmp_path / "elsewhere"


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


def test_probe_stores_its_choices(store: Store):
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
        "SELECT seed_id, question, criteria, options, correct_idx, explanation"
        " FROM probes WHERE id = ?",
        (probe_id,),
    ).fetchone()
    import json

    assert row["seed_id"] == seed_id
    assert row["question"].startswith("What would happen")
    assert json.loads(row["options"]) == list(a_probe().options)
    assert row["correct_idx"] == 0
    assert row["explanation"].startswith("Within the dedupe window")
    # The column is legacy-frozen: new probes write an empty list into it.
    assert json.loads(row["criteria"]) == []


class TestDurationIsPersisted:
    """Every model-calling stage already times itself; the number has to land.

    `cost_usd` is stored per stage for a reason — a single column meaning
    different things per verdict is a column nobody can sum. Duration follows
    the same split, on the same three tables, so "what did this stage cost in
    money and in wall time" is one query rather than two sources.
    """

    def a_session(self, store: Store, **kwargs) -> None:
        store.record_session(
            session_id="0198e4f1",
            transcript_path="/tmp/0198e4f1.jsonl",
            cwd=None,
            git_branch=None,
            verdict="ask",
            **kwargs,
        )

    def test_a_session_stores_triage_duration(self, store: Store):
        self.a_session(store, duration_ms=1200)
        row = store.conn.execute("SELECT duration_ms FROM sessions").fetchone()
        assert row["duration_ms"] == 1200

    def test_a_session_without_a_duration_stores_null(self, store: Store):
        """Stage 0 records silence without ever calling a model, so it has none."""
        self.a_session(store)
        row = store.conn.execute("SELECT duration_ms FROM sessions").fetchone()
        assert row["duration_ms"] is None

    def test_a_seed_stores_its_duration(self, store: Store):
        self.a_session(store)
        seed_id = store.add_seed(a_seed())
        row = store.conn.execute(
            "SELECT duration_ms FROM seeds WHERE id = ?", (seed_id,)
        ).fetchone()
        assert row["duration_ms"] == 900

    def test_a_probe_stores_its_duration(self, store: Store):
        self.a_session(store)
        probe_id = store.add_probe(store.add_seed(a_seed()), a_probe())
        row = store.conn.execute(
            "SELECT duration_ms FROM probes WHERE id = ?", (probe_id,)
        ).fetchone()
        assert row["duration_ms"] == 1300

    def test_a_seed_that_was_never_timed_stores_null(self, store: Store):
        """`duration_ms` is None on any Completion whose envelope omitted it."""
        self.a_session(store)
        seed_id = store.add_seed(replace(a_seed(), duration_ms=None))
        row = store.conn.execute(
            "SELECT duration_ms FROM seeds WHERE id = ?", (seed_id,)
        ).fetchone()
        assert row["duration_ms"] is None


def test_a_seed_needs_a_session(store: Store):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.add_seed(a_seed(session_id="never-recorded"))


def test_schema_is_applied_twice_without_error(tmp_path: Path):
    path = tmp_path / "grask.db"
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

    def test_migration_adds_duration_to_every_timed_table(self, tmp_path: Path):
        """The migration is no longer probes-only, so it has to reach all three."""
        with Store(self.legacy_db(tmp_path)) as store:
            for table in ("sessions", "seeds", "probes"):
                cols = {r[1] for r in store.conn.execute(f"PRAGMA table_info({table})")}
                assert "duration_ms" in cols, table

    def test_a_legacy_row_gains_a_null_duration(self, tmp_path: Path):
        """Backfilling a time that was never measured would be inventing data."""
        with Store(self.legacy_db(tmp_path)) as store:
            row = store.conn.execute("SELECT duration_ms FROM seeds").fetchone()
        assert row["duration_ms"] is None

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

    def test_a_legacy_probe_is_invisible_to_probe_by_id_too(self, tmp_path: Path):
        """Nothing converts these rows anymore, so both read paths must refuse them.

        The live database still holds one. `options IS NOT NULL` on both queries
        is the only thing standing between it and a serve that would hand the
        developer a probe with no options.
        """
        with Store(self.legacy_db(tmp_path)) as store:
            assert store.probe_by_id(1) is None


class TestNextProbe:
    """Selection is newest-unasked-unexpired, and all three words are load-bearing."""

    def stored(self, store: Store, *, session_id: str, created_at: str, topic: str) -> int:
        """Write a session, seed, and probe, forcing `probes.created_at`."""
        store.record_session(
            session_id=session_id,
            transcript_path=f"/tmp/{session_id}.jsonl",
            cwd="/tmp",
            git_branch="main",
            verdict="ask",
        )
        seed_id = store.add_seed(replace(a_seed(session_id), topic=topic))
        probe_id = store.add_probe(seed_id, a_probe())
        store.conn.execute(
            "UPDATE probes SET created_at = ? WHERE id = ?", (created_at, probe_id)
        )
        store.conn.commit()
        return probe_id

    def test_returns_none_on_an_empty_store(self, store: Store):
        assert store.next_probe() is None

    def test_returns_the_newest(self, store: Store):
        self.stored(store, session_id="old", created_at=iso_days_ago(3), topic="the old one")
        newest = self.stored(
            store, session_id="new", created_at=iso_days_ago(1), topic="the new one"
        )

        pending = store.next_probe()

        assert pending is not None
        assert pending.probe_id == newest
        assert pending.rubric.topic == "the new one"

    def test_skips_a_probe_that_was_already_asked(self, store: Store):
        older = self.stored(store, session_id="old", created_at=iso_days_ago(3), topic="older")
        newer = self.stored(store, session_id="new", created_at=iso_days_ago(1), topic="newer")
        store.record_ask(an_interrogation(probe_id=newer))

        pending = store.next_probe()

        assert pending is not None
        assert pending.probe_id == older

    def test_skips_a_probe_older_than_the_ttl(self, store: Store):
        self.stored(
            store,
            session_id="stale",
            created_at=iso_days_ago(PROBE_TTL_DAYS + 1),
            topic="stale",
        )

        assert store.next_probe() is None

    def test_keeps_a_probe_inside_the_ttl(self, store: Store):
        self.stored(
            store,
            session_id="fresh",
            created_at=iso_days_ago(PROBE_TTL_DAYS - 1),
            topic="fresh",
        )

        assert store.next_probe() is not None

    def test_carries_the_whole_rubric(self, store: Store):
        """Topic and hypothesis live on the seed, not on `probes`."""
        self.stored(store, session_id="one", created_at=iso_days_ago(1), topic="the topic")

        pending = store.next_probe()

        assert pending is not None
        assert pending.rubric.topic == "the topic"
        assert pending.rubric.hypothesis == a_seed().hypothesis
        assert pending.question == a_probe().question

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


class TestCapturingMarker:
    """The in-flight state: a session that ended but has not finished capturing.

    The bug this exists for: `/grask` in a still-open window, seconds after
    another window closed, reported "you're caught up — more after your next
    session" about a probe that was thirty seconds from being written. The queue
    could not tell "nothing to ask" from "not yet".
    """

    def test_a_marked_session_reports_capturing(self, store: Store):
        store.begin_session(session_id="live", transcript_path="/t/live.jsonl")

        assert store.next_probe() is None
        assert store.empty_reason() == "capturing"

    def test_the_verdict_replaces_the_marker(self, store: Store):
        store.begin_session(session_id="live", transcript_path="/t/live.jsonl")
        store.record_session(
            session_id="live",
            transcript_path="/t/live.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="silent",
            cost_usd=0.02,
        )

        row = store.conn.execute(
            "SELECT verdict, cwd, cost_usd FROM sessions WHERE session_id = 'live'"
        ).fetchone()
        assert row["verdict"] == "silent"
        assert row["cwd"] == "/repo"
        assert row["cost_usd"] == 0.02
        assert store.empty_reason() == "never"

    def test_a_terminal_verdict_is_still_immutable(self, store: Store):
        """The marker is the one row a verdict may overwrite. Idempotency for
        every other row is what stands between a re-fired hook and paying twice.
        """
        store.record_session(
            session_id="done",
            transcript_path="/t/done.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="silent",
        )
        store.record_session(
            session_id="done",
            transcript_path="/t/done.jsonl",
            cwd="/elsewhere",
            git_branch="other",
            verdict="ask",
            topic="a topic that must not land",
        )

        row = store.conn.execute(
            "SELECT verdict, cwd, topic FROM sessions WHERE session_id = 'done'"
        ).fetchone()
        assert row["verdict"] == "silent"
        assert row["cwd"] == "/repo"
        assert row["topic"] is None

    def test_a_fresh_marker_blocks_a_second_capture(self, store: Store):
        store.begin_session(session_id="live", transcript_path="/t/live.jsonl")

        assert store.has_session("live") is True

    def test_a_stale_marker_stops_claiming_anything(self, store: Store):
        """A worker killed mid-flight must not jam its session forever, nor keep
        promising a probe that is never coming."""
        store.begin_session(session_id="dead", transcript_path="/t/dead.jsonl")
        stale = datetime.now(timezone.utc) - timedelta(minutes=CAPTURE_STALE_MINUTES + 1)
        store.conn.execute(
            "UPDATE sessions SET triaged_at = ? WHERE session_id = 'dead'",
            (stale.isoformat(),),
        )
        store.conn.commit()

        assert store.has_session("dead") is False
        assert store.empty_reason() == "never"

    def test_capturing_outranks_expiry(self, store: Store):
        """Both are temporary; only one of them ends in a question."""
        TestEmptyReason().stored(
            store, session_id="old", created_at=iso_days_ago(PROBE_TTL_DAYS + 1)
        )
        store.begin_session(session_id="live", transcript_path="/t/live.jsonl")

        assert store.empty_reason() == "capturing"


class TestEmptyReason:
    """The five ways an empty queue can be empty, kept distinguishable."""

    def stored(self, store: Store, *, session_id: str, created_at: str) -> int:
        return TestNextProbe().stored(
            store, session_id=session_id, created_at=created_at, topic="t"
        )

    def test_an_untouched_store_never_captured_anything(self, store: Store):
        assert store.empty_reason() == "never"

    def test_a_fully_answered_queue_is_caught_up(self, store: Store):
        probe_id = self.stored(store, session_id="one", created_at=iso_days_ago(1))
        store.record_ask(an_interrogation(probe_id=probe_id))

        assert store.empty_reason() == "caught_up"

    def test_an_unasked_stale_probe_is_expired(self, store: Store):
        self.stored(store, session_id="one", created_at=iso_days_ago(PROBE_TTL_DAYS + 1))

        assert store.empty_reason() == "expired"

    def test_expiry_outranks_caught_up(self, store: Store):
        """A queue holding both should report the one still costing the developer."""
        answered = self.stored(store, session_id="one", created_at=iso_days_ago(1))
        store.record_ask(an_interrogation(probe_id=answered))
        self.stored(store, session_id="two", created_at=iso_days_ago(PROBE_TTL_DAYS + 1))

        assert store.empty_reason() == "expired"

    def test_an_over_cap_row_is_over_cap_not_caught_up(self, store: Store):
        probe_id = self.stored(store, session_id="one", created_at=iso_days_ago(0))
        store.conn.execute(
            "UPDATE probes SET options = ? WHERE id = ?",
            (json.dumps([f"option {n}" for n in range(5)]), probe_id),
        )
        store.conn.commit()

        assert store.empty_reason(max_options=4) == "over_cap"

    def test_over_cap_outranks_expiry(self, store: Store):
        """It is the only reason with somewhere else to go: the terminal path."""
        wide = self.stored(store, session_id="one", created_at=iso_days_ago(0))
        store.conn.execute(
            "UPDATE probes SET options = ? WHERE id = ?",
            (json.dumps([f"option {n}" for n in range(5)]), wide),
        )
        store.conn.commit()
        self.stored(store, session_id="two", created_at=iso_days_ago(PROBE_TTL_DAYS + 1))

        assert store.empty_reason(max_options=4) == "over_cap"

    def test_an_uncapped_caller_never_reports_over_cap(self, store: Store):
        """No cap, no over-cap row — the terminal path can serve any width."""
        probe_id = self.stored(store, session_id="one", created_at=iso_days_ago(0))
        store.conn.execute(
            "UPDATE probes SET options = ? WHERE id = ?",
            (json.dumps([f"option {n}" for n in range(5)]), probe_id),
        )
        store.conn.commit()

        assert store.empty_reason() == "caught_up"

    def test_a_recent_session_whose_probe_failed_verification_says_so(self, store: Store):
        """`unverified` is its own reason for the same cause `capturing` was.

        Without it a session that produced a question and threw it away is
        indistinguishable from one that never had anything worth asking — and
        the developer is told "you're caught up" about work grask silently
        judged unaskable.
        """
        store.record_session(
            session_id="one",
            transcript_path="/tmp/one.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="unverified",
        )

        assert store.empty_reason() == "unverified"

    def test_an_old_unverified_session_stops_being_the_reason(self, store: Store):
        store.record_session(
            session_id="one",
            transcript_path="/tmp/one.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="unverified",
        )
        store.conn.execute(
            "UPDATE sessions SET triaged_at = ?", (iso_days_ago(PROBE_TTL_DAYS + 1),)
        )
        store.conn.commit()

        assert store.empty_reason() == "never"

    def test_capturing_outranks_unverified(self, store: Store):
        """A probe still being written beats one already discarded: it is the
        only one of the two the developer can do anything but wait for."""
        store.record_session(
            session_id="one",
            transcript_path="/tmp/one.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="unverified",
        )
        store.begin_session(session_id="two", transcript_path="/tmp/two.jsonl")

        assert store.empty_reason() == "capturing"

    def test_a_later_probe_retires_an_unverified_session_as_the_reason(self, store: Store):
        """Answering a newer probe means the discard is no longer the last word.

        Age alone was not enough. `unverified` outranks `caught_up`, so a single
        discard suppressed "you are caught up" for the whole seven days — the
        developer who then earned a probe and answered it was still told the
        queue was empty because a question had been thrown away, about a session
        that was no longer the last one.
        """
        store.record_session(
            session_id="one",
            transcript_path="/tmp/one.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="unverified",
        )
        probe_id = self.stored(store, session_id="two", created_at=iso_days_ago(0))
        store.conn.execute(
            "INSERT INTO asks (probe_id, asked_at, outcome, turns) VALUES (?, ?, ?, ?)",
            (probe_id, iso_days_ago(0), "correct", 1),
        )
        store.conn.commit()

        assert store.next_probe() is None
        assert store.empty_reason() == "caught_up"

    def test_an_unverified_session_after_the_last_probe_is_still_the_reason(self, store: Store):
        """The other side of the same rule: newer than every probe, so it stands."""
        self.stored(store, session_id="two", created_at=iso_days_ago(3))
        store.record_session(
            session_id="one",
            transcript_path="/tmp/one.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="unverified",
        )
        store.conn.execute("DELETE FROM asks")
        store.conn.execute("UPDATE probes SET options = NULL")
        store.conn.commit()

        assert store.next_probe() is None
        assert store.empty_reason() == "unverified"

    def test_an_unverified_session_does_not_hide_a_servable_probe(self, store: Store):
        """`empty_reason` only ever explains an empty queue; a pending probe
        must still be the thing that happens."""
        self.stored(store, session_id="one", created_at=iso_days_ago(0))
        store.record_session(
            session_id="two",
            transcript_path="/tmp/two.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="unverified",
        )

        assert store.next_probe() is not None

    def test_a_row_invisible_for_another_reason_counts_as_caught_up(self, store: Store):
        """Options stored NULL: probes exist, so `never` would be the falser claim."""
        probe_id = self.stored(store, session_id="one", created_at=iso_days_ago(1))
        store.conn.execute("UPDATE probes SET options = NULL WHERE id = ?", (probe_id,))
        store.conn.commit()

        assert store.next_probe() is None
        assert store.empty_reason() == "caught_up"


class TestRecordAsk:
    def a_probe_id(self, store: Store) -> int:
        store.record_session(
            session_id="0198e4f1",
            transcript_path="/tmp/0198e4f1.jsonl",
            cwd="/tmp",
            git_branch="main",
            verdict="ask",
        )
        return store.add_probe(store.add_seed(a_seed()), a_probe())

    def test_round_trips_an_interrogation(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(an_interrogation(probe_id=probe_id))

        row = store.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        assert row["outcome"] == "passed"
        assert row["confidence"] == 95
        assert row["turns"] == 1
        assert row["cost_usd"] == 0.0
        assert row["completed_at"] is not None

    def test_stores_a_row_per_turn_with_its_question(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(an_interrogation(probe_id=probe_id, turn_count=2))

        rows = store.conn.execute(
            "SELECT * FROM answers WHERE ask_id = ? ORDER BY turn", (ask_id,)
        ).fetchall()
        assert [r["turn"] for r in rows] == [0, 1]
        assert rows[1]["question"] == "follow-up 1"

    def test_one_ask_per_probe(self, store: Store):
        """The UNIQUE constraint is what makes 'unasked = no asks row' true."""
        probe_id = self.a_probe_id(store)
        store.record_ask(an_interrogation(probe_id=probe_id))

        with pytest.raises(sqlite3.IntegrityError):
            store.record_ask(an_interrogation(probe_id=probe_id))

    def test_a_skip_records_no_answers_and_a_null_confidence(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(
            Interrogation(
                probe_id=probe_id,
                outcome="skipped",
                confidence=None,
                objection=None,
                turns=(),
                cost_usd=0.0,
            )
        )

        row = store.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        assert row["confidence"] is None
        assert row["turns"] == 0
        assert store.conn.execute(
            "SELECT count(*) c FROM answers WHERE ask_id = ?", (ask_id,)
        ).fetchone()["c"] == 0

    def test_an_objection_survives(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(
            Interrogation(
                probe_id=probe_id,
                outcome="premise_rejected",
                confidence=None,
                objection="the question misreads the diff",
                turns=(),
                cost_usd=0.0,
            )
        )

        row = store.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        assert row["objection"] == "the question misreads the diff"


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
