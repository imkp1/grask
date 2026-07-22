"""The whole pipeline, end to end, for one session.

Runs detached from a SessionEnd hook with nothing watching its exit code. That
single fact decides the error handling: an exception here reaches no one, so
every failure has to become a row and a log line instead. `capture_session` does
not raise. If it ever does, a session ends and grill silently forgets it.

Order is extract → triage → seed → probe, cheapest first. Stage 0 is free and
filters sessions with no human in them; triage is one call and filters the
majority; only what survives both pays for stages 2 and 3.
"""

from __future__ import annotations

import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from grill.dialogue import extract_dialogue as _extract_dialogue
from grill.probe import probe as _probe
from grill.seed import seed as _seed
from grill.select import select
from grill.storage import Store, grill_home
from grill.transcript import extract
from grill.triage import triage as _triage


def log(message: str) -> None:
    """Append to the capture log. Never raises — this is the failure path itself."""
    try:
        path = grill_home() / "grill.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass


def capture_session(
    transcript_path: Path,
    store: Store,
    *,
    triage=_triage,
    seed=_seed,
    probe=_probe,
    extract_dialogue=_extract_dialogue,
) -> None:
    """Triage one ended session and persist whatever it earned.

    The stages are injectable so the control flow can be tested without spending
    money on a model. Defaults are the real thing.
    """
    session_id = Path(transcript_path).stem
    try:
        if store.has_session(session_id):
            return

        session = extract(Path(transcript_path))

        if not session.turns:
            # Stage 0's floor. No human said anything, so there is nothing to ask
            # about and no reason to pay triage to confirm it.
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict="silent",
            )
            return

        verdict = triage(session)

        # triage() never raises; it reports failure by returning silent with
        # `.error` set. Recording that as silence would make a broken model call
        # look like a boring session, and the failure-rate number would be a lie.
        if verdict.error:
            log(f"{session_id} triage error: {verdict.error}")
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict="error",
                cost_usd=verdict.cost_usd,
                duration_ms=verdict.duration_ms,
            )
            return

        if not verdict.kept:
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict="silent",
                cost_usd=verdict.cost_usd,
                duration_ms=verdict.duration_ms,
            )
            return

        # The verdict carries the selected moment's fields but not the Moment
        # itself, and stage 2 needs the object. `select` is pure, so re-running it
        # over the same moments returns the same one triage chose.
        moment = select(verdict.moments)
        if moment is None:
            raise RuntimeError("verdict is 'ask' but no moment survives selection")

        dialogue = extract_dialogue(Path(transcript_path))
        the_seed = seed(dialogue, moment)
        the_probe = probe(the_seed, dialogue)

        store.record_session(
            session_id=session.session_id,
            transcript_path=str(transcript_path),
            cwd=session.cwd,
            git_branch=session.git_branch,
            verdict="ask",
            signal=verdict.signal,
            topic=verdict.topic,
            cost_usd=verdict.cost_usd,
            duration_ms=verdict.duration_ms,
        )
        seed_id = store.add_seed(the_seed)
        store.add_probe(seed_id, the_probe)

    except Exception:
        log(f"{session_id} capture failed:\n{traceback.format_exc()}")
        try:
            store.record_session(
                session_id=session_id,
                transcript_path=str(transcript_path),
                cwd=None,
                git_branch=None,
                verdict="error",
            )
        except Exception:
            log(f"{session_id} could not even record the error:\n{traceback.format_exc()}")


def main(argv: list[str] | None = None) -> int:
    """The detached worker: `python -m grill.capture <transcript_path>`."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        log("worker started with no transcript path")
        return 0
    try:
        with Store() as store:
            capture_session(Path(args[0]), store)
    except Exception:
        log(f"worker failed before capture:\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
