"""The whole pipeline, end to end, for one session.

Runs detached from a SessionEnd hook with nothing watching its exit code. That
single fact decides the error handling: an exception here reaches no one, so
every failure has to become a row and a log line instead. `capture_session` does
not raise. If it ever does, a session ends and grask silently forgets it.

Order is extract → triage → seed → probe → verify, cheapest first. Stage 0 is
free and filters sessions with no human in them; triage is one call and filters
the majority; only what survives both pays for stages 2, 3 and 4.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from grask.dialogue import extract_dialogue as _extract_dialogue
from grask.llm import LLMError
from grask.probe import probe as _probe
from grask.seed import seed as _seed
from grask.select import select
from grask.storage import UNVERIFIED, Store, grask_home
from grask.transcript import extract
from grask.triage import triage as _triage
from grask.verify import ProbeUnverified
from grask.verify import verify as _verify


def log(message: str) -> None:
    """Append to the capture log. Never raises — this is the failure path itself."""
    try:
        path = grask_home() / "grask.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
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
    verify=_verify,
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

        # Before anything that costs time or money. The four model calls below
        # take ~45s, and until this row exists a session that just ended is
        # indistinguishable from a session that never happened — which is how
        # `/grask` in a still-open window comes back "you're caught up" about a
        # probe that is forty-five seconds from existing.
        store.begin_session(session_id=session_id, transcript_path=str(transcript_path))

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

        try:
            the_probe = verify(the_probe)
        except ProbeUnverified as exc:
            # A judgment, and the only thing that throws a question away. The
            # seed is still stored — the moment was real, triage and stage 2
            # were already paid for, and what failed is the question written on
            # top of them.
            #
            # `discarded_usd` is what stages 3 and 4 cost to reach that
            # judgment. Nothing else records it: there is no probes row on this
            # path, so without the column the most expensive half of a
            # discarded session reads as free — in the very report used to
            # decide whether this stage is worth its price. Kept apart from
            # `cost_usd` so that total stays exact rather than inferred.
            log(f"{session_id} probe unverified: {exc.reason}")
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict=UNVERIFIED,
                signal=verdict.signal,
                topic=verdict.topic,
                cost_usd=verdict.cost_usd,
                duration_ms=verdict.duration_ms,
                discarded_usd=exc.cost_usd,
            )
            store.add_seed(the_seed)
            return
        except LLMError as exc:
            # A call failure, and it keeps the probe. Verification is a check on
            # a question that already exists; a CLI that cannot run it has said
            # nothing about that question, and treating silence as a rejection
            # would empty the queue every time the model was unreachable.
            log(f"{session_id} verification unavailable, probe kept: {exc}")

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
    """The detached worker: `python -m grask.capture <transcript_path>`."""
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
