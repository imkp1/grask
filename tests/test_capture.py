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

from grask.capture import capture_session
from grask.llm import LLMError
from grask.probe import Probe, Rubric
from grask.seed import Seed
from grask.storage import Store
from grask.triage import Moment, TriageVerdict


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "grask.db") as s:
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
        duration_ms=1200,
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


class TestDurationReachesTheRow:
    """Each stage times itself; capture is the only thing that can persist it.

    Cost is already threaded down all four `record_session` paths. Duration has
    to follow the same paths or the two numbers disagree about which sessions
    they cover — and the error path is the one that most needs a wall time,
    since a triage failure is usually a timeout.
    """

    def test_an_ask_verdict_stores_all_three_durations(self, store: Store, tmp_path: Path):
        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
        )

        def duration(table: str) -> int | None:
            row = store.conn.execute(f"SELECT duration_ms FROM {table}").fetchone()
            return row["duration_ms"]

        assert duration("sessions") == 1200
        assert duration("seeds") == 900
        assert duration("probes") == 1300

    def test_a_silent_verdict_stores_its_duration(self, store: Store, tmp_path: Path):
        def silent(session):
            return TriageVerdict(
                session_id=session.session_id,
                verdict="silent",
                reason="nothing here",
                cost_usd=0.03,
                duration_ms=800,
            )

        capture_session(transcript(tmp_path, "ship it"), store, triage=silent)

        row = store.conn.execute("SELECT duration_ms FROM sessions").fetchone()
        assert row["duration_ms"] == 800

    def test_a_triage_error_stores_its_duration(
        self, store: Store, tmp_path: Path, monkeypatch
    ):
        """A failed call still burned wall time, and that is the number worth having."""
        monkeypatch.setenv("GRASK_HOME", str(tmp_path))

        def failed(session):
            return TriageVerdict(
                session_id=session.session_id,
                verdict="silent",
                reason="",
                cost_usd=0.02,
                duration_ms=30000,
                error="model call failed",
            )

        capture_session(transcript(tmp_path, "ship it"), store, triage=failed)

        row = store.conn.execute("SELECT verdict, duration_ms FROM sessions").fetchone()
        assert row["verdict"] == "error"
        assert row["duration_ms"] == 30000

    def test_a_session_with_no_human_turns_has_no_duration(
        self, store: Store, tmp_path: Path
    ):
        """Stage 0 never calls a model, so there is no time to record."""
        capture_session(transcript(tmp_path), store, triage=lambda session: None)

        row = store.conn.execute("SELECT duration_ms FROM sessions").fetchone()
        assert row["duration_ms"] is None


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
    monkeypatch.setenv("GRASK_HOME", str(tmp_path))

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
    assert "model call failed" in (tmp_path / "grask.log").read_text(encoding="utf-8")


def test_unexpected_exception_is_also_contained(store: Store, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRASK_HOME", str(tmp_path))

    def boom(session):
        raise ValueError("something nobody predicted")

    capture_session(transcript(tmp_path, "ship it"), store, triage=boom)

    assert store.conn.execute("SELECT verdict FROM sessions").fetchone()["verdict"] == "error"
    assert "something nobody predicted" in (tmp_path / "grask.log").read_text(encoding="utf-8")


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
    monkeypatch.setenv("GRASK_HOME", str(tmp_path))
    capture_session(tmp_path / "gone.jsonl", store)
    assert (tmp_path / "grask.log").exists()
