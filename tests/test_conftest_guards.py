"""Tests for the suite-wide guardrails themselves.

The leak this covers actually happened: adding stage 4 to `capture_session` gave
eight existing tests a real model call they never asked for, because they inject
every stage *except* the new one and the default is the real thing. The suite
still passed. The only visible symptom was the runtime going from 0.1s to 50s,
which is exactly the kind of signal nobody reads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_capture import a_probe, a_seed, ask_verdict, transcript

import grask.llm
from grask.capture import capture_session
from grask.llm import complete
from grask.storage import Store


def test_a_real_model_call_from_a_test_is_refused():
    with pytest.raises(BaseException, match="real model call"):
        complete("what is the airspeed velocity of an unladen swallow?")


def test_the_other_spelling_is_refused_too():
    """`llm.py` uses `run`, but the stage this guard is for is not written yet.

    A guard escapable by reaching for `Popen` holds only until someone reaches
    for `Popen`. `check_output` and friends route through `run`, so wrapping the
    two covers the module.
    """
    with pytest.raises(BaseException, match="real model call"):
        subprocess.Popen(["/usr/local/bin/claude", "-p", "hi"])

    with pytest.raises(BaseException, match="real model call"):
        subprocess.check_output(["claude", "-p", "hi"])


@pytest.mark.calibration
def test_a_calibration_test_may_reach_the_real_cli():
    """The one test that is supposed to spend money.

    The guard went in with stage 4 and refused every `claude` launch in the
    suite, `calibration` included — so `test_capture_smoke` stopped proving the
    four stages compose and started failing on the guard instead. Nothing
    noticed, because it is deselected by default and the failure only appears
    under `-m calibration`, which is the one run nobody does casually.

    Deselected by default like every other calibration test, and free: it
    asserts the guard is absent rather than launching anything.

    It asserts on `__name__` and not on identity against `subprocess.run`:
    `grask.llm.subprocess` *is* the `subprocess` module, so the fixture's
    `monkeypatch.setattr` moves both sides of that comparison at once and it
    holds whether the guard is installed or not.
    """
    assert grask.llm.subprocess.run.__name__ == "run"
    assert grask.llm.subprocess.Popen.__name__ != "refuse"


def test_a_subprocess_that_is_not_the_model_still_runs():
    """`install.py` probes a python version and `capture_run.py` spawns the
    detached worker. A guard those had to be remembered around is the thing
    this file exists to avoid needing."""
    assert subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout.strip() == "ok"


def test_the_refusal_survives_captures_error_containment(tmp_path: Path):
    """The guard has to outrank `capture_session`'s `except Exception`.

    Capture runs detached with nothing reading its exit code, so it turns every
    failure into a row rather than an exception — including, if the guard were
    an ordinary `Exception`, the guard itself. A stage-5 author would then see
    an `error` verdict instead of a message naming the problem, and only if
    their test asserted on the verdict at all.
    """
    # SIM117 wants these combined; the parenthesized form that would allow it is
    # 3.9+, and this suite runs on the 3.8 floor in CI.
    with Store(tmp_path / "guard.db") as store:  # noqa: SIM117
        with pytest.raises(BaseException, match="real model call"):
            capture_session(
                transcript(tmp_path, "why do we need an idempotency key here?"),
                store,
                triage=lambda session: ask_verdict(),
                seed=lambda dialogue, moment: a_seed(),
                probe=lambda seed, dialogue: a_probe(),
                # verify= deliberately omitted: this is the stage-5 mistake.
            )
