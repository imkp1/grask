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
from dataclasses import replace
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

# When grask.log is rotated to grask.log.1, discarding whatever was in .1
# already. One generation, because the log is a debugging aid for the capture
# that just failed and not an audit trail — a second generation would double the
# ceiling to hold traffic nobody reads.
#
# A megabyte is a few thousand capture lines, which on the observed rate (a
# handful per session) is months. The point is not to save disk; it is that a
# file appended to by a detached worker on every session end, forever, with
# nothing that ever truncates it, is unbounded by construction — and the tracebacks
# it holds are the biggest lines it writes.
MAX_LOG_BYTES = 1_048_576


def _rotate(path: Path) -> None:
    """Move an oversized log aside. Silent on failure, like everything here."""
    try:
        if path.stat().st_size >= MAX_LOG_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def log(message: str) -> None:
    """Append to the capture log. Never raises — this is the failure path itself."""
    try:
        path = grask_home() / "grask.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
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
        #
        # It is also the claim, and losing it is the only correct reason to
        # return silently: `has_session` above is a separate statement, so two
        # workers can both pass it and only this settles which of them proceeds.
        if not store.begin_session(
            session_id=session_id, transcript_path=str(transcript_path)
        ):
            log(f"{session_id} already being captured elsewhere; nothing to do")
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

        the_seed = None
        try:
            the_seed = seed(dialogue, moment)
            the_probe = probe(the_seed, dialogue)
        except LLMError as exc:
            # Stage 2 or 3 gave up. This used to fall through to the catch-all
            # below, which writes an `error` row with no cost and no seed —
            # three model calls' worth of spend recorded as $0.00, on the most
            # ordinary failure the pipeline has (probe exhausting its retries).
            #
            # The seed is stored whenever stage 2 got one. It is not a partial
            # result: the moment was real and the hypothesis is as good as it
            # would have been had stage 3 succeeded. `reprobe` is what picks it
            # back up.
            log(f"{session_id} stage {'3' if the_seed else '2'} failed: {exc}")
            store.record_session(
                session_id=session.session_id,
                transcript_path=str(transcript_path),
                cwd=session.cwd,
                git_branch=session.git_branch,
                verdict="error",
                signal=verdict.signal,
                topic=verdict.topic,
                cost_usd=verdict.cost_usd,
                duration_ms=verdict.duration_ms,
                discarded_usd=exc.cost_usd,
            )
            if the_seed is not None:
                store.add_seed(the_seed)
            return

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
            #
            # The attempts still cost something, and the probe row is where
            # that lands — keeping the question does not make the failed
            # verification free.
            #
            # Both columns or neither. `verify` folds stage 4 into `cost_usd`
            # and `duration_ms` together on the success path, for the reason it
            # states: two columns on one row covering different sets of stages
            # are two numbers nobody can put beside each other. Taking only the
            # money here reintroduced exactly that, on the one population where
            # it misleads most — the probes stage 4 was billed for and did not
            # get to judge.
            log(f"{session_id} verification unavailable, probe kept: {exc}")
            if exc.cost_usd is not None:
                the_probe = replace(the_probe, cost_usd=exc.cost_usd)
            if exc.duration_ms is not None:
                the_probe = replace(the_probe, duration_ms=exc.duration_ms)

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
