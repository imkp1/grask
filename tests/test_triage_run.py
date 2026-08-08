"""Tests for the corpus runner's report.

No LLM is called here, and none is needed: `report` is a pure function over
rows, which is the whole reason the runner splits it out from `main`.

What these pin down is the report answering the question it exists to answer.
Counting kept sessions says whether stage 1 fires; it does not say *which*
signal fired, and it does not say why a moment was thrown away. Those are
different findings with opposite implications — a signal that never appears is
a prompt problem, and a signal that appears and is always rejected is a gate
problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from grask.transcript import Session, Turn
from grask.triage import TriageVerdict
from grask.triage_run import _row, report


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "0198e4f1",
        "verdict": "ask",
        "signal": "asked_why",
        "topic": "idempotency keys",
        "quote": "why an idempotency key?",
        "reason": "the developer asking why this key is needed",
        "demoted_from_ask": False,
        "weak_evidence": False,
        "candidates": 1,
        "moments": [
            {
                "turn": 0,
                "signal": "asked_why",
                "topic": "idempotency keys",
                "quote": "why an idempotency key?",
                "weak_evidence": False,
            }
        ],
        "rejections": [],
        "cost_usd": 0.01,
        "duration_ms": 1200,
        "error": None,
        "turns": 4,
        "files": 1,
        "branch": "main",
    }
    base.update(overrides)
    return base


class TestTheSignalHistogram:
    def test_every_signal_found_is_counted(self):
        rows = [
            row(),
            row(
                session_id="0198e4f2",
                signal="explained_it_back",
                moments=[
                    {
                        "turn": 2,
                        "signal": "explained_it_back",
                        "topic": "idempotency keys",
                        "quote": "the key stops duplicates because redis remembers",
                        "weak_evidence": False,
                    }
                ],
            ),
        ]

        out = report(rows)

        assert "explained_it_back" in out
        assert "asked_why" in out

    def test_a_signal_that_never_fired_is_reported_as_zero(self):
        """The finding this runner exists for right now.

        A signal missing from the output reads as "not measured". A signal
        printed as 0 reads as "measured, never fired" — which is the answer to
        whether rank 0 is rare, and it is only visible if absent signals are
        printed rather than skipped.
        """
        out = report([row()])

        assert "explained_it_back" in out

    def test_a_moment_that_lost_selection_is_still_counted(self):
        """Selection keeps one moment per session; prevalence is about all of
        them. A rank-0 moment that lost to nothing — it cannot — or a
        `pushed_back` that lost to `asked_why` is still evidence the signal
        fires at all."""
        out = report(
            [
                row(
                    candidates=2,
                    moments=[
                        {
                            "turn": 0,
                            "signal": "asked_why",
                            "topic": "idempotency keys",
                            "quote": "why an idempotency key?",
                            "weak_evidence": False,
                        },
                        {
                            "turn": 1,
                            "signal": "pushed_back",
                            "topic": "ledgers",
                            "quote": "no, use a ledger",
                            "weak_evidence": False,
                        },
                    ],
                )
            ]
        )

        assert "pushed_back" in out


class TestTheRejectionTally:
    def test_a_gate_rejection_is_reported_with_its_reason(self):
        rejection = (
            "turn 1: signal is explained_it_back but the quote asks rather than explains"
        )

        out = report([row(rejections=[rejection])])

        assert "quote asks rather than explains" in out

    def test_identical_rejections_across_sessions_are_counted_together(self):
        reason = "turn 1: signal is explained_it_back but shows names nothing wrong"
        out = report([row(rejections=[reason]), row(session_id="0198e4f2", rejections=[reason])])

        assert "shows names nothing wrong" in out
        assert "2" in out

    def test_the_turn_number_does_not_split_one_reason_into_two(self):
        """`turn N:` prefixes every rejection, so tallying the raw strings would
        report one occurrence each of the same gate firing twice."""
        out = report(
            [
                row(
                    rejections=[
                        "turn 1: signal is asked_why but the quote asks nothing",
                        "turn 7: signal is asked_why but the quote asks nothing",
                    ]
                )
            ]
        )

        assert out.count("the quote asks nothing") == 1


class TestTheRowCarriesRejections:
    def test_rejections_reach_the_row(self):
        """The report can only tally what `_row` serialises, and the JSON it
        writes is the artifact a later run is read against."""
        session = Session(
            session_id="0198e4f1",
            path=Path("/tmp/0198e4f1.jsonl"),
            git_branch="main",
            turns=[Turn(text="ship it", index=0)],
            files_touched=set(),
        )
        verdict = TriageVerdict(
            session_id="0198e4f1",
            verdict="ask",
            signal="asked_why",
            rejections=["turn 1: quote not found in that turn"],
        )

        assert _row(session, verdict)["rejections"] == [
            "turn 1: quote not found in that turn"
        ]
