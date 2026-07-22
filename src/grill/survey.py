"""Survey the local transcript corpus.

Diagnostic only — no LLM, no stored state. Answers "what is stage 0 actually
working with?" before any question-generation exists to spend money on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from grill.transcript import Session, extract, find_transcripts


def load_corpus(root: Path | None = None) -> list[Session]:
    sessions = []
    for path in find_transcripts(root):
        try:
            sessions.append(extract(path))
        except OSError:
            continue
    return sessions


def report(sessions: list[Session]) -> str:
    if not sessions:
        return "No transcripts found."

    raw = sum(s.raw_bytes for s in sessions)
    extracted = sum(s.extracted_bytes for s in sessions)
    with_turns = [s for s in sessions if s.turns]
    with_questions = [s for s in sessions if s.questions]

    lines = [
        "=" * 72,
        f"CORPUS: {len(sessions)} transcripts",
        "=" * 72,
        f"  raw            {raw / 1_048_576:>9.1f} MB",
        f"  extracted      {extracted / 1_048_576:>9.2f} MB"
        f"   ({raw / max(extracted, 1):.0f}x reduction)",
        f"  mean/session   {extracted / len(sessions) / 1024:>9.1f} KB",
        "",
        f"  sessions with any human turn   {len(with_turns):>4d}  "
        f"({len(with_turns) / len(sessions):.0%})",
        f"  sessions with a why-question   {len(with_questions):>4d}  "
        f"({len(with_questions) / len(sessions):.0%})",
        "",
        "Empty sessions are the triage floor: grill can never ask about these,",
        "so they cost nothing regardless of how stage 1 is implemented.",
        "",
        "-" * 72,
        "LARGEST BY EXTRACTED SIZE (the ones stage 1 must actually judge)",
        "-" * 72,
    ]

    for session in sorted(sessions, key=lambda s: s.extracted_bytes, reverse=True)[:15]:
        lines.append("  " + session.summary())

    biggest = max(sessions, key=lambda s: s.extracted_bytes)
    lines += [
        "",
        "-" * 72,
        f"SAMPLE QUESTIONS from {biggest.session_id[:8]}",
        "-" * 72,
    ]
    for turn in biggest.questions[:8]:
        text = " ".join(turn.text.split())
        lines.append(f"  [{turn.index:3d}] {text[:110]}")

    return "\n".join(lines)


def code_cost(sessions: list[Session]) -> str:
    """Measure the real input cost: the code a probe must be grounded in.

    Human turns are ~0.8 KB/session and effectively free. The files those turns
    touched are not, and this is what decides what stage 2 can afford to pull in.

    Counts whole current files, which is an upper bound in one direction and an
    underestimate in another: grill needs the session's *diff*, not the file, but
    a file may also have changed since. This measures the naive "read what was
    touched" strategy, which is the one to beat.
    """
    active = [s for s in sessions if s.turns and s.files_touched]
    if not active:
        return "No sessions with both human turns and edited files."

    rows = []
    for session in active:
        total = 0
        missing = 0
        for path_str in session.files_touched:
            try:
                total += Path(path_str).stat().st_size
            except OSError:
                missing += 1
        rows.append((session, total, missing))

    rows.sort(key=lambda r: r[1], reverse=True)
    sizes = [size for _, size, _ in rows]
    prompt_bytes = sum(s.extracted_bytes for s, _, _ in rows)
    code_bytes = sum(sizes)

    lines = [
        "",
        "=" * 72,
        f"CODE COST: {len(active)} sessions that edited files",
        "=" * 72,
        f"  human turns    {prompt_bytes / 1024:>9.1f} KB total",
        f"  files touched  {code_bytes / 1024:>9.1f} KB total"
        f"   ({code_bytes / max(prompt_bytes, 1):.0f}x the prompts)",
        f"  median/session {sorted(sizes)[len(sizes) // 2] / 1024:>9.1f} KB",
        f"  worst session  {max(sizes) / 1024:>9.1f} KB",
        "",
        "-" * 72,
        "HEAVIEST (session | files | code | prompts)",
        "-" * 72,
    ]
    for session, size, missing in rows[:10]:
        note = f"  [{missing} gone]" if missing else ""
        lines.append(
            f"  {session.session_id[:8]}  files={len(session.files_touched):3d}  "
            f"code={size / 1024:>8.1f}KB  prompts={session.extracted_bytes / 1024:>5.1f}KB{note}"
        )
    return "\n".join(lines)


def main() -> None:
    root = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    sessions = load_corpus(root)
    print(report(sessions))
    print(code_cost(sessions))


if __name__ == "__main__":
    main()
