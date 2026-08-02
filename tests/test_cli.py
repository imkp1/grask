"""Tests for the entry point.

`ask` is injected, so nothing here calls a model. What is worth pinning down is
the wiring and the two exits a developer hits by accident: an empty queue, and
Ctrl-C. The second one matters more than it looks — a probe is consumed by the
`asks` row that records it, so an interrupt that recorded a skip would destroy
the question on a stray keypress.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from grask.ask import Interrogation, PendingProbe
from grask.cli import EMPTY_QUEUE_NOTES, TERMINAL_EMPTY_NOTES, main
from grask.probe import Probe, Rubric
from grask.seed import Seed
from grask.storage import EmptyReason, Store

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
    def __init__(self, pending: PendingProbe | None, reason: str = "never") -> None:
        self.pending = pending
        self.reason = reason
        self.recorded: list[Interrogation] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def next_probe(self):
        return self.pending

    def empty_reason(self, **kwargs):
        return self.reason

    def record_ask(self, interrogation):
        self.recorded.append(interrogation)
        return 1


def an_interrogation() -> Interrogation:
    return Interrogation(
        probe_id=7,
        outcome="passed",
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


@pytest.mark.parametrize("reason", sorted(TERMINAL_EMPTY_NOTES))
def test_an_empty_queue_prints_the_note_for_its_reason(reason: str, capsys):
    """Each state gets its own line: "caught up" and "never captured" are not
    the same news, and a developer reading one when the other is true goes
    looking for a bug in the capture hook."""
    store = FakeStore(None, reason=reason)

    code = main([], store_factory=lambda: store, ask=lambda *a, **k: an_interrogation())

    assert code == 0
    assert capsys.readouterr().out.strip() == TERMINAL_EMPTY_NOTES[reason]


def test_the_terminal_notes_cover_every_empty_reason():
    """A reason with no note would be a KeyError on an empty queue."""
    assert set(TERMINAL_EMPTY_NOTES) == set(get_args(EmptyReason))
    assert set(EMPTY_QUEUE_NOTES) == set(get_args(EmptyReason))


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


def test_shim_writes_the_runner_shim(tmp_path: Path, capsys):
    """SessionStart runs this; it must leave an executable shim in grask's home
    (GRASK_HOME, redirected to tmp_path by conftest) that re-enters grask through
    the given plugin root."""
    code = main(["shim", "--root", str(tmp_path / "plugin-root")])
    captured = capsys.readouterr()

    shim = tmp_path / "grask"
    assert code == 0
    assert shim.is_file()
    assert str(tmp_path / "plugin-root") in shim.read_text(encoding="utf-8")
    # A SessionStart hook's stdout is injected into the session's context and
    # shown to the developer. A success line here would be a notification about
    # plumbing in every session forever, charged to work that has nothing to do
    # with grask. Only the failure has anything to say, and it says it on stderr.
    assert captured.out == ""
    assert captured.err == ""


def test_shim_never_fails_a_session_open(tmp_path: Path, capsys, monkeypatch):
    """A shim it cannot write is a warning, not a non-zero exit — SessionStart
    returning non-zero would surface an error on a surface the developer did not
    ask about."""
    def boom(root):
        raise OSError("read-only home")

    monkeypatch.setattr("grask.cli.write_runner_shim", boom)
    code = main(["shim", "--root", str(tmp_path / "plugin-root")])

    assert code == 0
    assert "could not write runner shim" in capsys.readouterr().err


class TestStats:
    """`grask stats` is the only view built for the person being asked.

    Everything else that reads the database is either a batch tool that spends
    money or a delivery surface that consumes a probe.
    """

    def _store(self, tmp_path: Path) -> Store:
        return Store(tmp_path / "grask.db")

    def _answered(self, store: Store, outcome: str, session_id: str) -> None:
        store.record_session(
            session_id=session_id,
            transcript_path="/t/a.jsonl",
            cwd="/repo",
            git_branch="main",
            verdict="ask",
        )
        seed_id = store.add_seed(
            Seed(
                session_id=session_id,
                turn=0,
                signal="asked_why",
                topic=RUBRIC.topic,
                quotes=("why?",),
                refs=(),
                decision="added the key",
                hypothesis=RUBRIC.hypothesis,
            )
        )
        probe_id = store.add_probe(
            seed_id,
            Probe(
                question=PENDING.question,
                options=PENDING.options,
                correct_idx=0,
                explanation=PENDING.explanation,
                rubric=RUBRIC,
            ),
        )
        store.record_ask(
            Interrogation(
                probe_id=probe_id,
                outcome=outcome,
                objection=None,
                turns=(),
                cost_usd=0.0,
            )
        )

    def test_a_fresh_install_says_nothing_answered_rather_than_printing_nothing(
        self, tmp_path: Path, capsys
    ):
        store = self._store(tmp_path)

        code = main(["stats"], store_factory=lambda: store)

        out = capsys.readouterr().out
        assert code == 0
        assert "Nothing answered yet" in out
        assert "answered            0" in out

    def test_it_shows_the_counts_and_what_was_asked(self, tmp_path: Path, capsys):
        store = self._store(tmp_path)
        self._answered(store, "passed", "s-1")
        self._answered(store, "failed", "s-2")

        main(["stats"], store_factory=lambda: store)

        out = capsys.readouterr().out
        assert "sessions seen       2" in out
        assert "(1 right, 1 wrong, 0 skipped)" in out
        assert RUBRIC.topic in out
        assert PENDING.question in out

    def test_it_never_prints_a_score(self, tmp_path: Path, capsys):
        """design.md: one probe cannot identify understanding. A percentage over
        a handful of questions asserts exactly that, and turns a twenty-second
        check into a number to protect."""
        store = self._store(tmp_path)
        self._answered(store, "passed", "s-1")

        main(["stats"], store_factory=lambda: store)

        out = capsys.readouterr().out
        assert "%" not in out
        assert "accuracy" not in out.lower() and "score" not in out.lower()

    def test_it_consumes_nothing(self, tmp_path: Path, capsys):
        """Reading your own record must not spend the queue."""
        store = self._store(tmp_path)
        self._answered(store, "passed", "s-1")
        before = store.conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0]

        main(["stats"], store_factory=lambda: store)

        with self._store(tmp_path) as reopened:
            after = reopened.conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0]
        assert after == before
