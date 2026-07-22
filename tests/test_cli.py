"""Tests for the entry point.

`ask` is injected, so nothing here calls a model. What is worth pinning down is
the wiring and the two exits a developer hits by accident: an empty queue, and
Ctrl-C. The second one matters more than it looks — a probe is consumed by the
`asks` row that records it, so an interrupt that recorded a skip would destroy
the question on a stray keypress.
"""

from __future__ import annotations

from pathlib import Path

from grill.ask import Interrogation, PendingProbe
from grill.cli import main
from grill.probe import Rubric
from grill.storage import Store

RUBRIC = Rubric(
    topic="idempotency of the retry path",
    hypothesis="the developer accepted the key without knowing what it dedupes against",
)

PENDING = PendingProbe(
    probe_id=7,
    question="What would break if the key were regenerated?",
    options=("It would stop deduping", "Nothing", "Requests would 409"),
    correct_idx=0,
    explanation="A fresh key per attempt dedupes nothing.",
    rubric=RUBRIC,
    created_at="2026-07-21T09:00:00+00:00",
)


class FakeStore:
    def __init__(self, pending: PendingProbe | None) -> None:
        self.pending = pending
        self.recorded: list[Interrogation] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def next_probe(self):
        return self.pending

    def record_ask(self, interrogation):
        self.recorded.append(interrogation)
        return 1


def an_interrogation() -> Interrogation:
    return Interrogation(
        probe_id=7,
        outcome="passed",
        confidence=95,
        objection=None,
        turns=(),
        cost_usd=0.02,
    )


def test_no_pending_probe_says_so_and_exits_zero(capsys):
    store = FakeStore(None)

    code = main([], store_factory=lambda: store, ask=lambda *a, **k: an_interrogation())

    assert code == 0
    assert capsys.readouterr().out.strip() != ""
    assert store.recorded == []


def test_records_the_interrogation(capsys):
    store = FakeStore(PENDING)
    seen = {}

    def ask(pending, console, **kwargs):
        seen["pending"] = pending
        return an_interrogation()

    code = main([], store_factory=lambda: store, ask=ask)

    assert code == 0
    assert seen["pending"] is PENDING
    assert len(store.recorded) == 1
    assert store.recorded[0].outcome == "passed"


def test_an_interrupt_records_nothing(capsys):
    """A stray Ctrl-C must not consume the probe.

    UNIQUE(probe_id) means an `asks` row is permanent, so recording an
    interrupt as a skip would destroy the question rather than defer it.
    """
    store = FakeStore(PENDING)

    def ask(pending, console, **kwargs):
        raise KeyboardInterrupt

    code = main([], store_factory=lambda: store, ask=ask)

    assert code == 130
    assert store.recorded == []


def test_the_real_store_sees_the_new_tables(tmp_path: Path):
    """The schema is additive and applied on open, so there is no migration step."""
    with Store(tmp_path / "grill.db") as store:
        names = {
            row["name"]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"asks", "answers"} <= names
    assert "criterion_results" not in names
