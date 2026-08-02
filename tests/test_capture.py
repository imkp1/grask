"""Tests for the capture orchestration.

No model is called here. Every stage is injected, so what these pin down is the
control flow: who gets recorded, what gets skipped, and — the one that matters —
that a stage blowing up produces an error row and a log line rather than an
exception. Capture runs detached with nothing watching its exit code, so an
exception it lets escape is a failure nobody ever learns about.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grask.capture import MAX_LOG_BYTES, capture_session, log
from grask.llm import LLMError
from grask.probe import Probe, Rubric
from grask.seed import Seed
from grask.storage import Store
from grask.triage import Moment, TriageVerdict
from grask.verify import ProbeUnverified


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


def test_the_session_is_marked_capturing_before_the_first_model_call(
    store: Store, tmp_path: Path
):
    """The window this closes: ~30s of three sequential model calls during which
    an ended session was indistinguishable from one that never happened, so a
    still-open window's `/grask` said "you're caught up" about a probe that was
    seconds away.

    Asserted from inside triage, which is the earliest thing that costs money.
    """
    seen = {}

    def triage(session):
        seen["reason"] = store.empty_reason()
        seen["verdict"] = store.conn.execute(
            "SELECT verdict FROM sessions"
        ).fetchone()["verdict"]
        return TriageVerdict(
            session_id=session.session_id, verdict="silent", reason="nothing here"
        )

    capture_session(transcript(tmp_path, "ship it"), store, triage=triage)

    assert seen["verdict"] == "capturing"
    assert seen["reason"] == "capturing"
    # And it does not outlive the run: the verdict replaces it, in one row.
    assert counts(store) == (1, 0, 0)
    assert store.empty_reason() == "never"


def test_a_crashed_stage_still_clears_the_capturing_marker(store: Store, tmp_path: Path):
    """Error containment has to cover the marker too. A marker left behind by a
    stage that raised would promise a probe that is never coming."""

    def explode(session):
        raise LLMError("triage fell over")

    capture_session(transcript(tmp_path, "ship it"), store, triage=explode)

    row = store.conn.execute("SELECT verdict FROM sessions").fetchone()
    assert row["verdict"] == "error"
    assert store.empty_reason() == "never"


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
        verify=lambda probe: probe,
    )

    assert counts(store) == (1, 1, 1)
    row = store.conn.execute("SELECT verdict, signal, topic FROM sessions").fetchone()
    assert row["verdict"] == "ask"
    assert row["signal"] == "asked_why"
    assert row["topic"] == "idempotency of the retry path"


class TestVerificationGuardsTheQueue:
    """Stage 4 decides whether a written probe reaches the developer.

    The asymmetry pinned here is the whole point: a *judgment* that the key does
    not survive throws the probe away, while a *call failure* keeps it. Collapse
    the two and a broken CLI silently empties the queue, which looks exactly
    like grask having nothing to ask.
    """

    # The surviving path is covered by `test_ask_verdict_stores_session_seed_and_probe`
    # above and by `test_the_verified_cost_is_what_gets_stored` below.

    def test_an_unverified_probe_is_not_queued(self, store: Store, tmp_path: Path):
        def reject(probe):
            raise ProbeUnverified("two options were judged true")

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=reject,
        )

        sessions, seeds, probes = counts(store)
        assert probes == 0
        # The seed is still worth keeping: the moment was real and the triage
        # spend already happened. Only the question is thrown away.
        assert (sessions, seeds) == (1, 1)

    def test_an_unverified_session_says_so_rather_than_ask_or_error(
        self, store: Store, tmp_path: Path
    ):
        def reject(probe):
            raise ProbeUnverified("no option was judged true")

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=reject,
        )

        assert (
            store.conn.execute("SELECT verdict FROM sessions").fetchone()["verdict"]
            == "unverified"
        )

    def test_a_verifier_call_failure_keeps_the_probe(self, store: Store, tmp_path: Path):
        def broken(probe):
            raise LLMError("claude exited 1")

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=broken,
        )

        assert counts(store) == (1, 1, 1)
        assert store.conn.execute("SELECT verdict FROM sessions").fetchone()["verdict"] == "ask"

    def test_the_verified_cost_is_what_gets_stored(self, store: Store, tmp_path: Path):
        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=lambda probe: replace(probe, cost_usd=0.17),
        )

        assert store.conn.execute("SELECT cost_usd FROM probes").fetchone()[0] == 0.17

    def test_a_discarded_probes_spend_is_still_recorded(self, store: Store, tmp_path: Path):
        """Throwing the question away does not make it retrospectively free.

        There is no probes row on this path, so the session row is the last
        place the stage 3 + stage 4 spend can land. Without it the most
        expensive part of a discarded session reads as $0.00 in the report used
        to decide whether stage 4 earns its price.

        The two columns stay separate so that `SUM(discarded_usd)` — what the
        stage has cost to produce nothing — is exact. Merged, it could only be
        estimated by assuming a triage cost back out.
        """

        def reject(probe):
            raise ProbeUnverified("no option was judged true", cost_usd=0.27, duration_ms=8000)

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=reject,
        )

        row = store.conn.execute("SELECT cost_usd, discarded_usd FROM sessions").fetchone()
        assert row["discarded_usd"] == 0.27
        # Not merged into triage's column, which is what keeps the discarded
        # total exact rather than something you infer by subtraction.
        assert row["cost_usd"] == 0.05


class TestAStageThatGaveUpStillReportsWhatItSpent:
    """Stage 2 and 3 raise `LLMError`; nothing used to catch it.

    It fell to `capture_session`'s catch-all, which writes an `error` row with
    no cost and no seed. Probe exhausting its three attempts is the most
    ordinary failure the pipeline has, and it recorded three billed calls as
    $0.00 while throwing away a stage 2 result that was never the problem.
    """

    def test_a_failed_probe_keeps_the_seed_it_was_written_from(
        self, store: Store, tmp_path: Path
    ):
        def exhausted(seed, dialogue):
            raise LLMError("probe exhausted its attempts", cost_usd=0.22)

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=exhausted,
        )

        sessions, seeds, probes = counts(store)
        assert (sessions, seeds, probes) == (1, 1, 0)
        row = store.conn.execute("SELECT verdict, cost_usd, discarded_usd FROM sessions").fetchone()
        assert row["verdict"] == "error"
        assert (row["cost_usd"], row["discarded_usd"]) == (0.05, 0.22)

    def test_a_failed_seed_has_no_seed_to_keep(self, store: Store, tmp_path: Path):
        def exhausted(dialogue, moment):
            raise LLMError("hypothesis missing or not a claim", cost_usd=0.11)

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=exhausted,
            probe=lambda seed, dialogue: a_probe(),
        )

        assert counts(store) == (1, 0, 0)
        row = store.conn.execute("SELECT verdict, discarded_usd FROM sessions").fetchone()
        assert (row["verdict"], row["discarded_usd"]) == ("error", 0.11)

    def test_a_failure_that_cannot_price_itself_records_no_cost(
        self, store: Store, tmp_path: Path
    ):
        """None is "not known", and must not be written as 0.0 — a CLI that
        never started spent nothing, which is a different fact."""

        def exhausted(seed, dialogue):
            raise LLMError("claude CLI not found on PATH")

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=exhausted,
        )

        assert store.conn.execute("SELECT discarded_usd FROM sessions").fetchone()[0] is None

    def test_a_kept_probe_carries_what_the_failed_verification_cost(
        self, store: Store, tmp_path: Path
    ):
        """The call failure keeps the probe — it does not make the attempts free."""

        def broken(probe):
            raise LLMError("claude exited 1", cost_usd=0.30)

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=broken,
        )

        assert counts(store) == (1, 1, 1)
        assert store.conn.execute("SELECT cost_usd FROM probes").fetchone()[0] == 0.30

    def test_a_kept_probe_carries_the_failed_verifications_duration_too(
        self, store: Store, tmp_path: Path
    ):
        """Both columns or neither.

        `verify` folds stage 4 into `cost_usd` and `duration_ms` together on the
        success path, because two columns on one row covering different sets of
        stages are two numbers nobody can put beside each other. Taking only the
        money here reintroduced exactly that — and did it on the one population
        where it misleads most, the probes stage 4 was billed for and never got
        to judge.
        """

        def broken(probe):
            raise LLMError("claude exited 1", cost_usd=0.30, duration_ms=9100)

        capture_session(
            transcript(tmp_path, "why do we need an idempotency key here?"),
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=broken,
        )

        row = store.conn.execute("SELECT cost_usd, duration_ms FROM probes").fetchone()
        assert (row["cost_usd"], row["duration_ms"]) == (0.30, 9100)


class TestOneWorkerPerSession:
    """`has_session` and `begin_session` are two statements, so two workers can
    both pass the first one. The claim has to be settled by the second.

    The window is real: SessionEnd can fire twice for one session, and
    `capture_run` walks the corpus while the hook is live. Both workers used to
    run the whole four-call pipeline and both used to write — `record_session`
    no-ops for the loser, but `add_seed` and `add_probe` had no such guard, so
    one session produced two seeds and two probes and the developer was asked
    the same question twice.
    """

    def _capture(self, store: Store, path: Path):
        capture_session(
            path,
            store,
            triage=lambda session: ask_verdict(),
            seed=lambda dialogue, moment: a_seed(),
            probe=lambda seed, dialogue: a_probe(),
            verify=lambda probe: probe,
        )

    def test_a_second_worker_past_the_guard_writes_nothing(
        self, store: Store, tmp_path: Path, monkeypatch
    ):
        path = transcript(tmp_path, "why do we need an idempotency key here?")
        # Both workers checked before either wrote a verdict. That is the race,
        # and it is the only way to reach the claim from two sides at once.
        monkeypatch.setattr(Store, "has_session", lambda self, session_id: False)

        self._capture(store, path)
        self._capture(store, path)

        assert counts(store) == (1, 1, 1)

    def test_the_loser_never_reaches_a_model(
        self, store: Store, tmp_path: Path, monkeypatch
    ):
        """Returning early is worth having only if it happens before the spend."""
        path = transcript(tmp_path, "why do we need an idempotency key here?")
        monkeypatch.setattr(Store, "has_session", lambda self, session_id: False)
        calls = []

        def counting_triage(session):
            calls.append(session.session_id)
            return ask_verdict()

        for _ in range(2):
            capture_session(
                path,
                store,
                triage=counting_triage,
                seed=lambda dialogue, moment: a_seed(),
                probe=lambda seed, dialogue: a_probe(),
                verify=lambda probe: probe,
            )

        assert len(calls) == 1

    def test_a_worker_that_died_mid_flight_can_be_taken_over(
        self, store: Store, tmp_path: Path, monkeypatch
    ):
        """The other direction. Refusing every conflict would jam a session
        forever behind a marker its worker never got to replace — which is the
        recovery path `has_session` already opens, and this must not close."""
        path = transcript(tmp_path, "why do we need an idempotency key here?")
        store.begin_session(session_id=path.stem, transcript_path=str(path))
        # Age the marker past the staleness window: the worker is gone.
        store.conn.execute(
            "UPDATE sessions SET triaged_at = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),),
        )
        store.conn.commit()

        self._capture(store, path)

        assert counts(store) == (1, 1, 1)
        assert store.conn.execute("SELECT verdict FROM sessions").fetchone()[0] == "ask"


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
            verify=lambda probe: probe,
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
        verify=lambda probe: probe,
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


class TestTheLogIsBounded:
    """A file a detached worker appends to on every session end, forever, with
    nothing that ever truncates it, is unbounded by construction — and the
    tracebacks it holds are the biggest lines it writes."""

    def test_an_oversized_log_is_rotated_before_the_next_write(self, tmp_path: Path):
        log_file = tmp_path / "grask.log"
        log_file.write_text("x" * (MAX_LOG_BYTES + 1), encoding="utf-8")

        log("the line that tipped it over")

        assert (tmp_path / "grask.log.1").stat().st_size == MAX_LOG_BYTES + 1
        assert log_file.read_text(encoding="utf-8").endswith("tipped it over\n")
        assert log_file.stat().st_size < MAX_LOG_BYTES

    def test_a_small_log_is_left_alone(self, tmp_path: Path):
        log("first")
        log("second")

        assert not (tmp_path / "grask.log.1").exists()
        assert len((tmp_path / "grask.log").read_text(encoding="utf-8").splitlines()) == 2

    def test_only_one_generation_is_kept(self, tmp_path: Path):
        """The log is a debugging aid for the capture that just failed, not an
        audit trail. A second generation doubles the ceiling to hold traffic
        nobody reads."""
        for marker in ("oldest", "newer"):
            (tmp_path / "grask.log").write_text(
                marker + "y" * MAX_LOG_BYTES, encoding="utf-8"
            )
            log("tip")

        assert (tmp_path / "grask.log.1").read_text(encoding="utf-8").startswith("newer")
