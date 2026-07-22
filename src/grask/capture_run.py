"""Run the capture pipeline over transcripts the hook never saw.

The SessionEnd hook only ever captures sessions that end from now on, and the
sessions that end from now on are overwhelmingly grask's own. Waiting for the
queue to fill measures grask on grask. This walks the corpus already on disk
instead, skips grask's own project by default, and pays for a bounded number of
real sessions from other work.

Costs money, so it does not spend by default: without `--go` it prints the plan
and an estimate and stops.

Usage:
    uv run python -m grask.capture_run [--limit N] [--go]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from grask.capture import capture_session
from grask.storage import Store
from grask.transcript import extract, find_transcripts

DEFAULT_WORKERS = 6
DEFAULT_LIMIT = 40

# Any transcript whose project directory contains one of these is not corpus.
# "grask" keeps the tool from being evaluated on its own construction; the
# scratchpad dirs are agent side-sessions with no developer in them at all.
DEFAULT_EXCLUDE = ("grask", "scratchpad")

# Measured across this runner's own batches, which is the only population that
# predicts this runner: 60 sessions selected, 25 kept, $16.36 spent.
#
# Deliberately one all-in number per session rather than a triage/seed/probe
# decomposition. The decomposition looks more principled and fits worse: the two
# observed batches ($1.62/10 and $14.74/50) cannot be reconciled by any single
# pair of per-stage costs, because session length varies more than stage price
# does. A model that cannot fit the two points it was built from should not be
# dressed up as three constants.
#
# The first version of this was fitted to n=14 from a different population (the
# hook's grask-only sessions) and under-quoted a 50-session run by 64%. Erring
# low is the harmful direction — it under-quotes spend the developer then
# authorises — so treat the band, not the point, as the estimate.
COST_PER_SESSION = 0.27
COST_BAND = 0.4  # observed batch error against the point estimate, both ways
KEPT_RATE = 0.42  # 25 of 60; drives the probe-count estimate, not the cost one


@dataclass
class Plan:
    """What a run would touch, and what it declined to."""

    selected: list[Path] = field(default_factory=list)
    excluded: int = 0
    already_captured: int = 0
    no_human_turns: int = 0
    over_limit: int = 0

    def estimate_usd(self) -> float:
        return len(self.selected) * COST_PER_SESSION

    def estimate_range_usd(self) -> tuple[float, float]:
        """The number to quote. A point estimate here has already misled once."""
        point = self.estimate_usd()
        return point * (1 - COST_BAND), point * (1 + COST_BAND)

    def estimate_probes(self) -> int:
        return round(len(self.selected) * KEPT_RATE)


def plan(
    paths: Sequence[Path],
    *,
    exclude: Sequence[str],
    is_captured: Callable[[str], bool],
    has_human_turns: Callable[[Path], bool],
    limit: int | None,
) -> Plan:
    """Decide which transcripts to spend on. Pure — every side effect is injected.

    The three filters are ordered by what they cost to evaluate: the project name
    is free, the captured check is one indexed read, and the human-turn check
    parses the whole file. The limit applies last, so `--limit 40` means forty
    sessions that will actually reach triage rather than forty rows of scratchpad.
    """
    result = Plan()
    for path in paths:
        project = path.parent.name
        if any(marker in project for marker in exclude):
            result.excluded += 1
            continue
        if is_captured(path.stem):
            result.already_captured += 1
            continue
        if not has_human_turns(path):
            result.no_human_turns += 1
            continue
        if limit is not None and len(result.selected) >= limit:
            result.over_limit += 1
            continue
        result.selected.append(path)
    return result


def has_human_turns(path: Path) -> bool:
    """Stage 0's filter, run early so empty sessions never eat the limit."""
    try:
        return bool(extract(path).turns)
    except OSError:
        return False


def capture_one(path: Path) -> None:
    """One session, one connection.

    sqlite3 connections are not shareable across threads, so each task opens its
    own. Writes are brief next to the model calls they follow, and the driver's
    default busy timeout absorbs the overlap.
    """
    with Store() as store:
        capture_session(path, store)


def outcomes(store: Store, session_ids: Sequence[str]) -> str:
    """Read back what the run actually produced.

    `capture_session` reports nothing — it is built to run detached, where the
    only honest channel is a row. So the report is a query, not a return value.
    """
    if not session_ids:
        return "Nothing ran."
    marks = ",".join("?" * len(session_ids))
    verdicts = store.conn.execute(
        f"SELECT verdict, COUNT(*) n, SUM(COALESCE(cost_usd, 0)) c "
        f"FROM sessions WHERE session_id IN ({marks}) GROUP BY verdict",
        tuple(session_ids),
    ).fetchall()
    probes = store.conn.execute(
        f"SELECT COUNT(*) n, SUM(COALESCE(p.cost_usd, 0) + COALESCE(s.cost_usd, 0)) c "
        f"FROM probes p JOIN seeds s ON s.id = p.seed_id "
        f"WHERE s.session_id IN ({marks})",
        tuple(session_ids),
    ).fetchone()

    spent = sum(row["c"] or 0.0 for row in verdicts) + (probes["c"] or 0.0)
    lines = [
        "=" * 72,
        f"CAPTURED {len(session_ids)} sessions",
        "=" * 72,
    ]
    for row in sorted(verdicts, key=lambda r: -r["n"]):
        lines.append(f"  {row['verdict']:<8} {row['n']:>4d}   ${row['c'] or 0.0:.2f}")
    lines += [
        "",
        f"  probes minted  {probes['n']:>4d}",
        f"  spent          ${spent:.2f}",
    ]
    if not probes["n"]:
        lines.append("")
        lines.append("  No probes. Either triage kept nothing or every kept session errored —")
        lines.append("  check grask.log before spending on a second batch.")
    return "\n".join(lines)


def describe(result: Plan, *, limit: int | None) -> str:
    low, high = result.estimate_range_usd()
    lines = [
        "=" * 72,
        f"BACKFILL PLAN: {len(result.selected)} sessions to capture",
        "=" * 72,
        f"  skipped, excluded project     {result.excluded:>5d}",
        f"  skipped, already captured     {result.already_captured:>5d}",
        f"  skipped, no human turns       {result.no_human_turns:>5d}",
        f"  skipped, over the limit       {result.over_limit:>5d}" if limit is not None else "",
        "",
        f"  estimated cost  ${low:.2f} - ${high:.2f}"
        f"   (~${COST_PER_SESSION:.2f}/session, +/-{COST_BAND:.0%})",
        f"  expected probes {result.estimate_probes():>5d}   (at {KEPT_RATE:.0%} kept)",
        "",
        "-" * 72,
    ]
    for path in result.selected:
        lines.append(f"  {path.stem[:8]}  {path.parent.name}")
    return "\n".join(line for line in lines if line != "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--root", type=Path, default=None, help="transcript root")
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help=f"project-name substring to skip; repeatable (default: {DEFAULT_EXCLUDE})",
    )
    parser.add_argument("--go", action="store_true", help="actually spend; otherwise dry run")
    args = parser.parse_args(argv)

    exclude = tuple(args.exclude) if args.exclude else DEFAULT_EXCLUDE
    with Store() as store:
        result = plan(
            find_transcripts(args.root),
            exclude=exclude,
            is_captured=store.has_session,
            has_human_turns=has_human_turns,
            limit=args.limit,
        )

    print(describe(result, limit=args.limit))
    if not result.selected:
        return 0
    if not args.go:
        print("\nDry run. Re-run with --go to spend.")
        return 0

    print(f"\ncapturing on {args.workers} workers...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(capture_one, result.selected))

    with Store() as store:
        print()
        print(outcomes(store, [p.stem for p in result.selected]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
