"""The full-transcript read that stages 2 and 3 need, and stage 1 must not have.

Stage 0 (`transcript.extract`) keeps developer turns and file paths — ~1.3KB per
session, which is the right input for deciding *whether* a session has anything
in it. It is the wrong input for deciding *what to ask*: a rubric grounded in a
file path is not grounded, and a probe that cannot quote the developer back to
themselves is a generic question.

So this reads the same file wider: assistant prose, and the before/after of every
edit. That is the expensive input the design's staging section is organised
around — read once, at the moment the transcript is freshest.

What does not widen is the human filter. Injected skill text and tool results are
still excluded, for the same reason as in stage 0: they are not the developer, and
a question about text nobody typed is the failure that gets this uninstalled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grask.transcript import (
    EDIT_TOOLS,
    Turn,
    human_turn_text,
    iter_records,
    message_content,
)

# Per side of one edit. A vendored blob or a generated client is the common
# reason a single edit is enormous, and none of it is the developer's thinking.
# Capping each side rather than the dialogue as a whole means a runaway edit
# costs its own slot and not its neighbours'.
MAX_EDIT_CHARS = 2000

# Replies are not capped here. They used to be, at 4000 — and never once took
# effect, because the only thing that reads a Reply is `seed.render_dialogue`,
# which clips every event to `MAX_EVENT_CHARS` (2000) on the way into the
# prompt. A tighter cap downstream makes the looser one upstream unreachable:
# `_clip(x, 4000)[:2000] == x[:2000]` for every string. Two numbers, one of
# which could never be the answer.
#
# Edits still are capped here, and that is not the same situation: an edit is
# rendered from `before`/`after`, which `render_dialogue` does not clip, so this
# is the only place a vendored blob gets stopped.

TRUNCATED = "\n… [truncated]"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATED


@dataclass
class Reply:
    """Something the agent said in prose. Not evidence of understanding.

    Kept because it is what the developer was responding to — the explanation
    they may have nodded along to without checking.
    """

    text: str
    index: int


@dataclass
class Edit:
    """A change that landed in the developer's codebase.

    `before` is None for a whole-file write, where there was nothing to replace.
    The distinction matters to a rubric: "why did this change" and "why does this
    exist" are different questions.
    """

    file_path: str
    before: str | None
    after: str
    index: int


@dataclass
class Dialogue:
    """One session, wide enough to ground a question in."""

    session_id: str
    path: Path
    cwd: str | None = None
    git_branch: str | None = None
    events: list[Turn | Reply | Edit] = field(default_factory=list)

    @property
    def turns(self) -> list[Turn]:
        return [e for e in self.events if isinstance(e, Turn)]

    @property
    def edits(self) -> list[Edit]:
        return [e for e in self.events if isinstance(e, Edit)]


def _edit_from_tool_use(block: dict[str, Any], index: int) -> Edit | None:
    if block.get("type") != "tool_use" or block.get("name") not in EDIT_TOOLS:
        return None
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path")
    if not isinstance(path, str):
        return None

    # Edit carries old_string/new_string; Write carries the whole content.
    before = tool_input.get("old_string")
    after = tool_input.get("new_string")
    if not isinstance(after, str):
        after = tool_input.get("content")
    if not isinstance(after, str):
        return None

    return Edit(
        file_path=path,
        before=_clip(before, MAX_EDIT_CHARS) if isinstance(before, str) else None,
        after=_clip(after, MAX_EDIT_CHARS),
        index=index,
    )


def extract_dialogue(path: Path) -> Dialogue:
    """Read a transcript into an ordered dialogue of turns, replies, and edits.

    Order is preserved across speakers because the interleaving carries meaning:
    an explanation given *after* the developer pushed back is evidence of
    something different from one given before, and a rubric that cannot tell
    those apart misattributes what they understood.
    """
    dialogue = Dialogue(session_id=path.stem, path=path)

    for record in iter_records(path):
        dialogue.cwd = dialogue.cwd or record.get("cwd")
        dialogue.git_branch = dialogue.git_branch or record.get("gitBranch")

        if (text := human_turn_text(record)) is not None:
            dialogue.events.append(Turn(text=text, index=len(dialogue.events)))
            continue

        content = message_content(record)
        if record.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                index = len(dialogue.events)
                if edit := _edit_from_tool_use(block, index):
                    dialogue.events.append(edit)
                elif block.get("type") == "text":
                    reply = block.get("text")
                    if isinstance(reply, str) and reply.strip():
                        dialogue.events.append(Reply(text=reply.strip(), index=index))

    return dialogue
