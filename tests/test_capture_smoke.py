"""One real run of the whole pipeline, against a real transcript.

Costs money — measured at $0.93 for a session that triages to `ask`, on a run
where stage 3 regenerated once. $0.59 is the floor, when the probe lands first
try. Marked `calibration` so the ordinary suite stays free and offline; run it
deliberately:

    .venv/bin/pytest -m calibration tests/test_capture_smoke.py -s

Every other test in this repo injects the stages. This is the only one that
proves the four of them actually compose, which is a different claim from each
of them working alone.

Point it at a transcript you already know triages to `ask`, via GRASK_SMOKE_TRANSCRIPT.
Worth doing rather than taking the default: without it this picks the newest
transcript on disk, which is the session you are running the test from, and a
session whose only content so far is running a test triages `silent` — a skip,
after paying for the triage call. A historical `ask` is not a guarantee either,
since the triage prompt moves and the call is stochastic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from grask.capture import capture_session
from grask.storage import Store
from grask.transcript import find_transcripts


def a_keep_transcript() -> Path:
    override = os.environ.get("GRASK_SMOKE_TRANSCRIPT")
    if override:
        return Path(override)
    found = find_transcripts()
    if not found:
        pytest.skip("no transcripts on disk to smoke-test against")
    return found[0]


@pytest.mark.calibration
def test_one_real_session_end_to_end(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("GRASK_HOME", str(tmp_path))
    path = a_keep_transcript()

    with Store(tmp_path / "grask.db") as store:
        capture_session(path, store)

        session = store.conn.execute(
            "SELECT session_id, verdict, signal, topic, cost_usd FROM sessions"
        ).fetchone()
        assert session is not None, "capture recorded nothing at all"
        assert session["verdict"] in {"ask", "silent"}, "capture errored — see grask.log"

        with capsys.disabled():
            print(f"\ntranscript: {path}")
            print(f"verdict:    {session['verdict']}  ({session['topic']})")

        if session["verdict"] != "ask":
            pytest.skip(f"transcript triaged {session['verdict']}; nothing further to check")

        seed = store.conn.execute("SELECT * FROM seeds").fetchone()
        probe = store.conn.execute("SELECT * FROM probes").fetchone()
        assert seed is not None and probe is not None

        assert seed["hypothesis"].strip()
        assert json.loads(seed["quotes"]), "a stored seed with no quotes broke the evidence rule"
        assert probe["question"].strip().endswith("?")
        assert probe["seed_id"] == seed["id"]

        # `criteria` is not checked: it is a NOT NULL column left from the judge
        # design, and `storage.add_probe` writes `[]` into it on every row. This
        # test used to assert it was non-empty, which no probe has satisfied
        # since the judge was cut — invisibly, because calibration is deselected
        # by default. What replaced it is the answer key, so that is what a real
        # run has to produce.
        options = json.loads(probe["options"])
        assert 3 <= len(options) <= 4, "the option gates let through an unusable count"
        assert len(set(options)) == len(options), "a duplicated option is a free elimination"
        assert probe["correct_idx"] in range(len(options)), "the key names no real option"
        assert probe["explanation"].strip(), "the payoff is the explanation"

        total = sum(
            row[0] or 0
            for row in [
                (session["cost_usd"],),
                (seed["cost_usd"],),
                (probe["cost_usd"],),
            ]
        )
        with capsys.disabled():
            print(f"question:   {probe['question']}")
            print(f"cost:       ${total:.2f}")
