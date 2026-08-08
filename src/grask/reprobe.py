"""Write a new question from a seed whose first one never reached anybody.

Two paths in `capture.py` store a seed and no probe: stage 4 discarding what
stage 3 wrote, and stage 2 succeeding into a stage 3 that gave up. Both keep the
seed deliberately — the moment was real, triage and stage 2 are already paid
for, and what failed is the question written on top of them. Until this module
nothing could act on that, which made "the seed is still stored" a consolation
rather than a recovery: a design that priced a control against a redemption
nobody had built.

Stages 3 and 4 again, not a cheaper shortcut. Re-asking without verifying would
reintroduce exactly the defect stage 4 exists to catch, on the population most
likely to carry it — these are the seeds that already failed once.

Explicit rather than automatic, and the reason is the same one `capture_run` and
`triage_run` are: it spends money. A retry folded into the next capture would
bill the developer for a decision they never made, and a seed that fails twice
would do it on a schedule. Without `--go` this prints the plan and stops.

Usage:
    uv run python -m grask.reprobe [--limit N] [--go]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from grask.capture import log
from grask.dialogue import extract_dialogue as _extract_dialogue
from grask.llm import LLMError
from grask.probe import probe as _probe
from grask.storage import PROBE_TTL_DAYS, Store, UnprobedSeed
from grask.verify import ProbeUnverified
from grask.verify import verify as _verify

DEFAULT_LIMIT = 10

# One stage 3 call plus one stage 4 call, from the measured figures in the
# design doc. Deliberately not the whole-session number `capture_run` quotes:
# triage and stage 2 have already been paid for on every seed here.
COST_PER_SEED = 0.27


@dataclass
class Outcome:
    """What one seed turned into."""

    seed_id: int
    topic: str
    result: str
    detail: str = ""


def _seeds(n: int) -> str:
    return "1 seed" if n == 1 else f"{n} seeds"


def eligible(store: Store, *, limit: int | None = DEFAULT_LIMIT) -> list[UnprobedSeed]:
    """Seeds worth spending on, newest first.

    A seed whose transcript has rotated away is dropped here rather than
    counted and skipped later: stage 3 needs the dialogue, so without the file
    there is nothing to re-ask and the developer should not see it in a plan
    they are being asked to authorise.
    """
    found = [s for s in store.unprobed_seeds() if s.transcript_path.exists()]
    return found if limit is None else found[:limit]


def reprobe_one(
    store: Store,
    candidate: UnprobedSeed,
    *,
    probe=_probe,
    verify=_verify,
    extract_dialogue=_extract_dialogue,
) -> Outcome:
    """Write and check one question, storing it only if stage 4 vouches for it.

    Never raises. This runs over a batch, and one seed whose transcript has
    become unreadable since `eligible` looked must not cost the rest of the run.

    A second discard is a result, not an error. It is also the useful signal
    the retry produces: a seed that cannot yield a verifiable question twice is
    more likely to be a bad seed than a bad roll.

    Stage 3 is re-run without being told why the first question was discarded,
    and that is a measured choice rather than an oversight. Feeding stage 4's
    reason back was built and reverted: on the two discarded probes whose
    transcripts survived, both arms recovered — the blind re-run included — so
    the premise that a re-run needs the reason was not observed to hold. The
    reason is still stored on the session; nothing has earned the right to put
    it in a prompt.
    """
    topic = candidate.seed.topic
    try:
        dialogue = extract_dialogue(candidate.transcript_path)
        the_probe = verify(probe(candidate.seed, dialogue))
    except ProbeUnverified as exc:
        log(f"reprobe seed {candidate.seed_id} unverified again: {exc.reason}")
        return Outcome(candidate.seed_id, topic, "unverified", exc.reason)
    except LLMError as exc:
        log(f"reprobe seed {candidate.seed_id} failed: {exc}")
        return Outcome(candidate.seed_id, topic, "failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - a batch must survive one bad file
        log(f"reprobe seed {candidate.seed_id} errored: {exc}")
        return Outcome(candidate.seed_id, topic, "error", str(exc))

    store.add_probe(candidate.seed_id, the_probe)
    return Outcome(candidate.seed_id, topic, "probed")


def plan(candidates: list[UnprobedSeed]) -> str:
    if not candidates:
        return (
            "No seeds are waiting for a question. Either nothing has been discarded\n"
            f"in the last {PROBE_TTL_DAYS} days, or the transcripts behind them have rotated away."
        )
    lines = [
        f"{_seeds(len(candidates))} with no question, and a transcript that still exists:",
        "",
    ]
    lines += [f"  {c.seed_id:>4d}  {c.seed.topic}" for c in candidates]
    lines += [
        "",
        f"Re-running stages 3 and 4 would cost about ${len(candidates) * COST_PER_SEED:.2f}.",
        "Nothing has been spent. Re-run with --go.",
    ]
    return "\n".join(lines)


def report(outcomes: list[Outcome]) -> str:
    if not outcomes:
        return "Nothing ran."
    tally: dict[str, int] = {}
    for outcome in outcomes:
        tally[outcome.result] = tally.get(outcome.result, 0) + 1

    lines = ["=" * 72, f"REPROBED {_seeds(len(outcomes))}", "=" * 72]
    lines += [f"  {name:<10} {count:>4d}" for name, count in sorted(tally.items())]
    detailed = [o for o in outcomes if o.detail]
    if detailed:
        lines.append("")
        lines += [f"  {o.seed_id:>4d}  {o.result}: {o.detail[:90]}" for o in detailed]
    if not tally.get("probed"):
        lines += [
            "",
            "  No seed produced a verifiable question. A second discard on the same",
            "  seed is worth reading as a fact about the seed, not a bad roll.",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--go", action="store_true", help="actually spend money")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    with Store() as store:
        candidates = eligible(store, limit=args.limit)
        if not args.go:
            print(plan(candidates))
            return 0
        print(report([reprobe_one(store, c) for c in candidates]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
