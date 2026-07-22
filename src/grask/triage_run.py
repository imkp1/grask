"""Run stage 1 over the local corpus and report what it kept.

The output that matters is not whether this executes — it is whether a developer
reading the kept/dropped split agrees with the calls. Sessions kept that were
actually empty are the nagging failure mode showing up before a question exists.

Usage:
    uv run python -m grask.triage_run [--limit N] [--out PATH] [--workers N]
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from grask.storage import grask_home
from grask.survey import load_corpus
from grask.transcript import Session
from grask.triage import TriageVerdict, triage

DEFAULT_WORKERS = 6


def _row(session: Session, verdict: TriageVerdict) -> dict[str, Any]:
    return {
        "session_id": verdict.session_id,
        "verdict": verdict.verdict,
        "signal": verdict.signal,
        "topic": verdict.topic,
        "quote": verdict.quote,
        "reason": verdict.reason,
        "demoted_from_ask": verdict.demoted_from_ask,
        "weak_evidence": verdict.weak_evidence,
        "candidates": verdict.candidates,
        "moments": [
            {
                "turn": m.turn,
                "signal": m.signal,
                "topic": m.topic,
                "quote": m.quote,
                "weak_evidence": m.weak_evidence,
            }
            for m in verdict.moments
        ],
        "cost_usd": verdict.cost_usd,
        "duration_ms": verdict.duration_ms,
        "error": verdict.error,
        "turns": len(session.turns),
        "files": len(session.files_touched),
        "branch": session.git_branch,
    }


def _clip(text: str | None, width: int) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def report(rows: list[dict[str, Any]]) -> str:
    kept = [r for r in rows if r["verdict"] == "ask"]
    dropped = [r for r in rows if r["verdict"] != "ask"]
    demoted = [r for r in dropped if r["demoted_from_ask"]]
    failed = [r for r in rows if r["error"]]
    weak = [r for r in kept if r.get("weak_evidence")]
    cost = sum(r["cost_usd"] or 0.0 for r in rows)

    lines = [
        "=" * 78,
        f"STAGE 1 TRIAGE: {len(rows)} sessions with human turns",
        "=" * 78,
        f"  kept (ask)     {len(kept):>4d}  ({len(kept) / max(len(rows), 1):.0%})",
        f"    of which evidence is code-grounded, not quoted  {len(weak):>3d}",
        f"  dropped        {len(dropped):>4d}  ({len(dropped) / max(len(rows), 1):.0%})",
        f"    of which demoted for a bad quote  {len(demoted):>3d}",
        f"    of which failed outright          {len(failed):>3d}",
        f"  cost           ${cost:.2f}   (${cost / max(len(rows), 1):.4f}/session)",
        f"  candidates     {sum(r['candidates'] for r in rows):>4d} moments across all sessions"
        f"   (max {max((r['candidates'] for r in rows), default=0)} in one)",
        "",
        "-" * 78,
        f"KEPT — {len(kept)} sessions grask would ask about",
        "-" * 78,
    ]
    for r in sorted(kept, key=lambda r: -r["turns"]):
        mark = " [WEAK]" if r.get("weak_evidence") else ""
        lines.append(
            f"  {r['session_id'][:8]}  turns={r['turns']:<3d} "
            f"{r.get('signal') or '?':<20}{mark}"
        )
        lines.append(f"            topic: {_clip(r['topic'], 62)}")
        lines.append(f"            quote: {_clip(r['quote'], 62)}")
        lines.append(f"            shows: {_clip(r['reason'], 62)}")
        if r["candidates"] > 1:
            others = [m for m in r["moments"] if m["topic"] != r["topic"]]
            lines.append(
                "            also: " + _clip("; ".join(m["topic"] for m in others), 62)
            )
        lines.append("")

    lines += [
        "-" * 78,
        f"DROPPED — {len(dropped)} sessions grask would stay silent on",
        "-" * 78,
    ]
    for r in sorted(dropped, key=lambda r: -r["turns"]):
        flag = " [DEMOTED]" if r["demoted_from_ask"] else (" [ERROR]" if r["error"] else "")
        lines.append(
            f"  {r['session_id'][:8]}  turns={r['turns']:<3d}{flag} "
            f"{_clip(r['error'] or r['reason'], 56)}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="only the N newest sessions")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    # Under GRASK_HOME, never the working directory. This output is verbatim
    # developer quotes pulled from every project on the machine; defaulting it
    # to cwd once put a 69-session corpus into this repository's git history.
    parser.add_argument("--out", type=Path, default=grask_home() / "triage-results.json")
    parser.add_argument("--root", type=Path, default=None, help="transcript root")
    args = parser.parse_args()

    sessions = [s for s in load_corpus(args.root) if s.turns]
    if args.limit:
        sessions = sessions[: args.limit]
    if not sessions:
        print("No sessions with human turns.")
        return

    print(f"triaging {len(sessions)} sessions on {args.workers} workers...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verdicts = list(pool.map(triage, sessions))

    # strict: pool.map preserves length and order, so a mismatch here is a bug in
    # that assumption rather than a short session list to be silently truncated.
    rows = [_row(s, v) for s, v in zip(sessions, verdicts, strict=True)]
    # Nothing else here opens a Store, so this is the only thing that would
    # create GRASK_HOME.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(report(rows))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
