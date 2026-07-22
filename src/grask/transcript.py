"""Stage 0: mechanical extraction of a Claude Code transcript.

No LLM, no cost. Turns a `.jsonl` session log into the small subset that carries
signal about what the developer engaged with.

The reduction is large — a 2.8MB transcript typically yields ~11KB here — because
the bulk of a session log is tool results, file snapshots, and injected skill text.
None of that is the developer thinking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# promptSource is the only field distinguishing a turn the developer authored from
# a tool result or injected skill/hook text, so this set is the load-bearing filter.
#
# Observed values across a 156-transcript corpus, and why each is in or out:
#   typed               (336)  in  — keyed by hand
#   suggestion_accepted  (72)  in  — developer chose it; skews interrogative
#   queued                (1)  in  — typed during a run, delivered later
#   sdk                  (41)  out — programmatic, no human present
#   system                (2)  out — harness-authored
#   absent             (5031)  out — tool results and injected text
#
# suggestion_accepted was excluded in the first pass and that was wrong: it is 18%
# of all human turns and disproportionately questions, which is the signal grask
# exists to find.
HUMAN_MARKERS = frozenset({"typed", "suggestion_accepted", "queued"})

EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")

# Subagent transcripts live one level deeper, under this directory name.
SUBAGENT_DIR = "subagents"


@dataclass
class Turn:
    """One thing the developer actually typed."""

    text: str
    timestamp: datetime | None
    index: int

    @property
    def is_question(self) -> bool:
        """Whether this turn reads as the developer interrogating rather than directing.

        Deliberately crude. This ranks candidates for the LLM stages; it does not
        decide anything on its own. Over-inclusive is the correct failure direction.
        """
        lowered = self.text.lower()
        return "?" in self.text or any(
            marker in lowered
            for marker in ("why ", "how does", "how do", "what if", "what happens")
        )


@dataclass
class Session:
    """The signal-bearing subset of one transcript."""

    session_id: str
    path: Path
    cwd: str | None = None
    git_branch: str | None = None
    turns: list[Turn] = field(default_factory=list)
    files_touched: set[str] = field(default_factory=set)
    raw_bytes: int = 0

    @property
    def extracted_bytes(self) -> int:
        return sum(len(t.text) for t in self.turns)

    @property
    def questions(self) -> list[Turn]:
        return [t for t in self.turns if t.is_question]

    def summary(self) -> str:
        ratio = self.raw_bytes / max(self.extracted_bytes, 1)
        return (
            f"{self.session_id[:8]}  turns={len(self.turns):3d}  "
            f"questions={len(self.questions):3d}  files={len(self.files_touched):3d}  "
            f"{self.raw_bytes // 1024:5d}KB -> {self.extracted_bytes // 1024:3d}KB "
            f"({ratio:.0f}x)"
        )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _file_from_tool_use(block: dict[str, Any]) -> str | None:
    """Pull a file path out of an assistant tool_use block, if it edited one."""
    if block.get("type") != "tool_use" or block.get("name") not in EDIT_TOOLS:
        return None
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path")
    return path if isinstance(path, str) else None


def extract(path: Path) -> Session:
    """Read a transcript and return only what matters.

    Malformed lines are skipped rather than raised on: transcripts are appended to
    by a live process, so the last line may be a partial write.
    """
    session = Session(session_id=path.stem, path=path, raw_bytes=path.stat().st_size)

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            session.cwd = session.cwd or record.get("cwd")
            session.git_branch = session.git_branch or record.get("gitBranch")

            record_type = record.get("type")
            content = record.get("message", {}).get("content") if isinstance(
                record.get("message"), dict
            ) else None

            if record_type == "user" and record.get("promptSource") in HUMAN_MARKERS:
                if isinstance(content, str) and content.strip():
                    session.turns.append(
                        Turn(
                            text=content.strip(),
                            timestamp=_parse_timestamp(record.get("timestamp")),
                            index=len(session.turns),
                        )
                    )

            elif record_type == "assistant" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and (found := _file_from_tool_use(block)):
                        session.files_touched.add(found)

    return session


def find_transcripts(root: Path | None = None) -> list[Path]:
    """Session transcripts on disk, newest first.

    Deliberately excludes `<session-id>/subagents/*.jsonl`. Subagent transcripts
    contain no human turns at all — a subagent talks to tools, never to the
    developer — so questioning someone on one asks about work they never saw.
    """
    root = root or Path.home() / ".claude" / "projects"
    if not root.exists():
        return []
    found = (p for p in root.glob("*/*.jsonl") if SUBAGENT_DIR not in p.parts)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
