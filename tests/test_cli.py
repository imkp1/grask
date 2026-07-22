"""Tests for the entry point.

`ask` is injected, so nothing here calls a model. What is worth pinning down is
the wiring and the two exits a developer hits by accident: an empty queue, and
Ctrl-C. The second one matters more than it looks — a probe is consumed by the
`asks` row that records it, so an interrupt that recorded a skip would destroy
the question on a stray keypress.
"""

from __future__ import annotations

from pathlib import Path

from grask.ask import Interrogation, PendingProbe
from grask.cli import main
from grask.probe import Rubric
from grask.storage import Store

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
    with Store(tmp_path / "grask.db") as store:
        names = {
            row["name"]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"asks", "answers"} <= names
    assert "criterion_results" not in names


def test_the_skill_ships_inside_the_package(tmp_path: Path, capsys):
    """`skill --install` must work from an installed wheel, not just a checkout.

    The file used to sit at the repo root, which reaches an sdist but never a
    wheel — so the documented "copy SKILL.md into your skills directory" step
    was unfollowable for anyone who installed rather than cloned. Reading it
    through `importlib.resources` is what makes the two cases identical.
    """
    code = main(["skill", "--install", "--dir", str(tmp_path)])
    capsys.readouterr()

    installed = tmp_path / "grask" / "SKILL.md"
    assert code == 0
    # The directory name is the slash command; `/grask` does not exist without it.
    assert installed.is_file()
    assert installed.read_text(encoding="utf-8").startswith("---\nname: grask\n")


def test_the_skill_prints_without_installing(tmp_path: Path, capsys):
    """Bare `grask skill` writes nothing — inspecting it is not installing it."""
    code = main(["skill", "--dir", str(tmp_path)])

    assert code == 0
    assert "name: grask" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
