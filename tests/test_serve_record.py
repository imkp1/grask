"""Tests for the non-interactive delivery seam: `grask serve` and `grask record`.

Real Store on a tmp database — the value under test is the wiring from argv to
SQLite and the exact JSON shapes Claude will parse. Blind serve is asserted on
raw stdout, not parsed keys: the answer key must not appear in any encoding.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grask.cli import EMPTY_QUEUE_NOTES, main
from grask.probe import Probe, Rubric
from grask.seed import Seed
from grask.storage import PROBE_TTL_DAYS, Store

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
    return tmp_path / "grask.db"


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

    def test_a_never_captured_queue_says_so(self, db: Path, capsys):
        Store(db).close()  # create the schema, store nothing

        code = run(db, ["serve", "--json"])

        assert code == 0
        assert json.loads(capsys.readouterr().out) == {
            "pending": None,
            "reason": "never",
            "note": EMPTY_QUEUE_NOTES["never"],
        }

    def test_an_answered_queue_says_caught_up_not_never_captured(self, db: Path, capsys):
        probe_id = stored_probe(db)
        run(db, ["record", str(probe_id), "--pick", "a"])
        capsys.readouterr()

        run(db, ["serve", "--json"])

        served = json.loads(capsys.readouterr().out)
        assert served["reason"] == "caught_up"
        assert served["note"] == EMPTY_QUEUE_NOTES["caught_up"]

    def test_an_expired_queue_says_expired(self, db: Path, capsys):
        stored_probe(db)
        with Store(db) as store:
            stale = (
                datetime.now(timezone.utc) - timedelta(days=PROBE_TTL_DAYS + 1)
            ).isoformat()
            store.conn.execute("UPDATE probes SET created_at = ?", (stale,))
            store.conn.commit()

        run(db, ["serve", "--json"])

        served = json.loads(capsys.readouterr().out)
        assert served["reason"] == "expired"
        assert served["note"] == EMPTY_QUEUE_NOTES["expired"]

    def test_a_five_option_row_is_left_pending_not_consumed(self, db: Path, capsys):
        wide = replace(
            a_probe(), options=tuple(f"option {n}" for n in range(5)), correct_idx=0
        )
        probe_id = stored_probe(db, probe=wide)

        code = run(db, ["serve", "--json"])

        assert code == 0
        # Empty for this surface only, and the note has to say so — the terminal
        # path can still ask a five-option row.
        assert json.loads(capsys.readouterr().out) == {
            "pending": None,
            "reason": "over_cap",
            "note": EMPTY_QUEUE_NOTES["over_cap"],
        }
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


class TestRecord:
    def test_the_correct_pick_passes_with_the_explanation(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--pick", "a"])

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        # Two fields and no third: `explanation` is inside `display` now, and a
        # second copy is a second thing that can disagree with the first.
        assert set(out) == {"outcome", "display"}
        assert out["outcome"] == "passed"
        assert "\u2713 Correct" in out["display"]
        assert a_probe().explanation in out["display"]
        rows = asks_rows(db)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "passed"
        assert rows[0]["confidence"] is None

    def test_an_uppercase_pick_is_the_same_pick(self, db: Path, capsys):
        # The delivery surface labels options "a)".."d)" and hands back the
        # letter it displayed, so an uppercase letter arrives on the normal
        # path. Rejecting it stranded an answer the developer had already given.
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--pick", "A"])

        assert code == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "passed"

    def test_a_wrong_pick_fails_and_stores_the_answer_text(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--pick", "b"])

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "failed"
        # The whole point of the change: a wrong pick is told which option was
        # right, in full, so the explanation does not have to be mapped back
        # onto a picker that is already gone.
        assert "\u2717 Incorrect" in out["display"]
        assert a_probe().options[0] in out["display"]
        assert a_probe().options[1] in out["display"]
        with Store(db) as store:
            answers = store.conn.execute("SELECT answer FROM answers").fetchall()
        assert [row["answer"] for row in answers] == [a_probe().options[1]]

    def test_skip_records_skipped(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--skip"])

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "skipped"
        # A skip is usually "I don't know", which is when the payoff is worth
        # most, and the probe is spent either way — so it still gets the answer
        # and the explanation, just no verdict word.
        assert "Skipped" in out["display"]
        assert a_probe().options[0] in out["display"]
        assert a_probe().explanation in out["display"]
        assert "Incorrect" not in out["display"]
        assert asks_rows(db)[0]["confidence"] is None

    def test_wrong_records_premise_rejected_with_the_objection(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(
            db,
            ["record", str(probe_id), "--wrong", "--objection", "that was the agent"],
        )

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "premise_rejected"
        # Disputing the question is not answering it: no key, no explanation.
        # Handing back the answer key would argue past the objection.
        assert a_probe().options[0] not in out["display"]
        assert a_probe().explanation not in out["display"]
        assert asks_rows(db)[0]["objection"] == "that was the agent"

    def test_wrong_without_an_objection(self, db: Path, capsys):
        probe_id = stored_probe(db)

        code = run(db, ["record", str(probe_id), "--wrong"])

        assert code == 0
        assert asks_rows(db)[0]["objection"] is None

    def test_a_double_record_is_rejected_without_a_second_row(self, db: Path, capsys):
        probe_id = stored_probe(db)
        run(db, ["record", str(probe_id), "--pick", "a"])
        capsys.readouterr()

        code = run(db, ["record", str(probe_id), "--pick", "b"])
        out = json.loads(capsys.readouterr().out)

        assert code == 1
        assert "error" in out
        assert len(asks_rows(db)) == 1

    def test_a_pick_beyond_the_stored_options_is_rejected_without_a_write(
        self, db: Path, capsys
    ):
        probe_id = stored_probe(db)  # three options: a-c

        code = run(db, ["record", str(probe_id), "--pick", "d"])
        out = json.loads(capsys.readouterr().out)

        assert code == 1
        assert "error" in out
        assert asks_rows(db) == []

    def test_an_unknown_probe_id_is_rejected(self, db: Path, capsys):
        Store(db).close()

        code = run(db, ["record", "999", "--pick", "a"])
        out = json.loads(capsys.readouterr().out)

        assert code == 1
        assert "error" in out

    def test_answering_needs_a_pick(self, db: Path, capsys):
        probe_id = stored_probe(db)

        with pytest.raises(SystemExit):
            run(db, ["record", str(probe_id)])
        assert asks_rows(db) == []
