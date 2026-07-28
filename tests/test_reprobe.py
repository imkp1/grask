"""Tests for the recovery path behind "the seed is still stored".

Two places in capture keep a seed and write no probe. Both were defensible only
if something could later pick the seed back up; until this module nothing could,
which is the shape of a control priced against a redemption nobody built.

What these pin is that the retry is a real re-run of stages 3 and 4 — not a
cheaper shortcut past the check that discarded the question in the first place,
on the one population most likely to fail it again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grask.llm import LLMError
from grask.probe import Probe, Rubric
from grask.reprobe import eligible, plan, report, reprobe_one
from grask.seed import Seed
from grask.storage import Store
from grask.verify import ProbeUnverified


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "grask.db") as opened:
        yield opened


def a_seed(session_id: str = "one") -> Seed:
    return Seed(
        session_id=session_id,
        turn=0,
        signal="asked_why",
        topic="idempotency of the retry path",
        quotes=("why do we need an idempotency key here?",),
        refs=("api/retry.py",),
        decision="added a key derived from the request body",
        hypothesis="They took at-least-once delivery on faith.",
        cost_usd=0.09,
    )


def a_probe() -> Probe:
    return Probe(
        question="What makes the retry safe?",
        options=("The key", "The clock"),
        correct_idx=0,
        explanation="The key deduplicates.",
        rubric=Rubric(topic="t", hypothesis="h"),
        cost_usd=0.26,
    )


def orphaned(store: Store, tmp_path: Path, *, session_id: str = "one", exists: bool = True) -> int:
    """A session that kept its seed and never got a probe."""
    path = tmp_path / f"{session_id}.jsonl"
    if exists:
        path.write_text("{}\n", encoding="utf-8")
    store.record_session(
        session_id=session_id,
        transcript_path=str(path),
        cwd="/repo",
        git_branch="main",
        verdict="unverified",
    )
    return store.add_seed(a_seed(session_id))


class TestEligible:
    def test_finds_a_seed_that_never_got_a_question(self, store: Store, tmp_path: Path):
        seed_id = orphaned(store, tmp_path)
        assert [c.seed_id for c in eligible(store)] == [seed_id]

    def test_reads_the_seed_back_whole(self, store: Store, tmp_path: Path):
        orphaned(store, tmp_path)
        recovered = eligible(store)[0].seed
        assert recovered == a_seed()

    def test_skips_a_seed_that_already_has_a_probe(self, store: Store, tmp_path: Path):
        seed_id = orphaned(store, tmp_path)
        store.add_probe(seed_id, a_probe())

        assert eligible(store) == []

    def test_skips_a_seed_whose_transcript_has_rotated_away(self, store: Store, tmp_path: Path):
        # Stage 3 needs the dialogue, not just the seed. Without the file there
        # is nothing to re-ask, so it must not appear in a plan the developer is
        # being asked to authorise.
        orphaned(store, tmp_path, exists=False)

        assert eligible(store) == []

    def test_honours_the_limit(self, store: Store, tmp_path: Path):
        for i in range(4):
            orphaned(store, tmp_path, session_id=f"s{i}")

        assert len(eligible(store, limit=2)) == 2


class TestReprobeOne:
    def test_a_verified_question_is_stored_against_the_original_seed(
        self, store: Store, tmp_path: Path
    ):
        seed_id = orphaned(store, tmp_path)
        candidate = eligible(store)[0]

        outcome = reprobe_one(
            store,
            candidate,
            probe=lambda seed, dialogue: a_probe(),
            verify=lambda probe: probe,
            extract_dialogue=lambda path: [],
        )

        assert outcome.result == "probed"
        row = store.conn.execute("SELECT seed_id, question FROM probes").fetchone()
        assert row["seed_id"] == seed_id
        assert row["question"] == "What makes the retry safe?"
        # And it is now servable, which is the whole point of the recovery.
        assert store.next_probe() is not None

    def test_the_new_question_is_verified_not_waved_through(self, store: Store, tmp_path: Path):
        """These seeds already failed once. Skipping stage 4 on exactly the
        population most likely to fail it again would be the wrong shortcut."""
        orphaned(store, tmp_path)
        seen = []

        def spy_verify(probe):
            seen.append(probe.question)
            return probe

        reprobe_one(
            store,
            eligible(store)[0],
            probe=lambda seed, dialogue: a_probe(),
            verify=spy_verify,
            extract_dialogue=lambda path: [],
        )

        assert seen == ["What makes the retry safe?"]

    def test_a_second_discard_stores_nothing_and_says_so(self, store: Store, tmp_path: Path):
        orphaned(store, tmp_path)

        def reject(probe):
            raise ProbeUnverified("two options were judged true")

        outcome = reprobe_one(
            store,
            eligible(store)[0],
            probe=lambda seed, dialogue: a_probe(),
            verify=reject,
            extract_dialogue=lambda path: [],
        )

        assert outcome.result == "unverified"
        assert "two options" in outcome.detail
        assert store.conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0] == 0

    def test_a_call_failure_stores_nothing_and_does_not_raise(self, store: Store, tmp_path: Path):
        orphaned(store, tmp_path)

        def broken(seed, dialogue):
            raise LLMError("claude exited 1")

        outcome = reprobe_one(
            store,
            eligible(store)[0],
            probe=broken,
            verify=lambda probe: probe,
            extract_dialogue=lambda path: [],
        )

        assert outcome.result == "failed"
        assert store.conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0] == 0

    def test_one_unreadable_transcript_does_not_end_the_batch(self, store: Store, tmp_path: Path):
        """`eligible` checked the file existed; that was some time ago."""
        orphaned(store, tmp_path)

        def gone(path):
            raise OSError("file vanished")

        outcome = reprobe_one(
            store,
            eligible(store)[0],
            probe=lambda seed, dialogue: a_probe(),
            verify=lambda probe: probe,
            extract_dialogue=gone,
        )

        assert outcome.result == "error"

    def test_a_recovered_probe_stops_the_queue_reporting_unverified(
        self, store: Store, tmp_path: Path
    ):
        """The session row stays `unverified` — terminal verdicts are immutable
        — but the reason is scoped to "nothing minted since", so a recovered
        probe retires it without anything having to rewrite history."""
        orphaned(store, tmp_path)
        assert store.empty_reason() == "unverified"

        reprobe_one(
            store,
            eligible(store)[0],
            probe=lambda seed, dialogue: a_probe(),
            verify=lambda probe: probe,
            extract_dialogue=lambda path: [],
        )

        assert store.next_probe() is not None


class TestReporting:
    def test_the_plan_spends_nothing_and_says_what_it_would(self, store: Store, tmp_path: Path):
        orphaned(store, tmp_path)
        text = plan(eligible(store))

        assert "idempotency of the retry path" in text
        assert "$0.27" in text
        assert "Nothing has been spent" in text

    def test_an_empty_plan_explains_both_ways_of_being_empty(self, store: Store):
        assert "rotated away" in plan([])

    def test_a_run_that_recovered_nothing_says_to_read_it_as_a_signal(self):
        from grask.reprobe import Outcome

        text = report([Outcome(1, "t", "unverified", "no option was judged true")])
        assert "not a bad roll" in text

    def test_nothing_ran_is_not_a_table(self):
        assert report([]) == "Nothing ran."
