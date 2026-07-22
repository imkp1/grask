"""Tests for the full-transcript read that stages 2 and 3 need.

Stage 0 keeps developer turns and file paths, which is right for triage and
useless here. Stage 3 has to quote the user back to themselves and ground a
rubric in code the developer actually wrote — neither is possible from a path.

What these pin down is that widening the read does not also widen it to the
thing stage 0 was built to exclude: injected skill text and tool results are
still not the developer, and an assistant turn is still not evidence of what
the developer understood. No LLM is called here.
"""

from __future__ import annotations

import json
from pathlib import Path

from grill.dialogue import Edit, Reply, extract_dialogue
from grill.transcript import Turn


def write_transcript(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "0198e4f1-0000-0000-0000-000000000000.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def human(text: str, source: str = "typed") -> dict:
    return {
        "type": "user",
        "promptSource": source,
        "timestamp": "2026-07-20T00:41:13.482Z",
        "cwd": "/Users/dev/projects/example",
        "gitBranch": "main",
        "message": {"role": "user", "content": text},
    }


def assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def assistant_edit(file_path: str, old: str, new: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "Edit",
                    "input": {"file_path": file_path, "old_string": old, "new_string": new},
                }
            ],
        },
    }


def tool_result(text: str) -> dict:
    return {
        "type": "user",
        "toolUseResult": {"stdout": text},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}],
        },
    }


def injected_skill(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


class TestWhatIsKept:
    def test_keeps_assistant_prose_that_stage_0_discards(self, tmp_path):
        """The explanation is the thing the developer may have accepted on faith.

        Stage 0 throws this away. It is the whole reason this module exists.
        """
        path = write_transcript(
            tmp_path,
            [
                human("why a mutex here?"),
                assistant_text("Because two goroutines write the same map."),
            ],
        )
        dialogue = extract_dialogue(path)

        assert [type(e) for e in dialogue.events] == [Turn, Reply]
        assert dialogue.events[1].text == "Because two goroutines write the same map."

    def test_keeps_edit_content_not_just_the_path(self, tmp_path):
        """A rubric grounded in a path is not grounded.

        `files_touched` can say `store.go` changed. Only the before/after says
        what changed, which is what a rubric has to be right about.
        """
        path = write_transcript(
            tmp_path,
            [human("fix the race"), assistant_edit("store.go", "m[k] = v", "mu.Lock()\nm[k] = v")],
        )
        dialogue = extract_dialogue(path)

        edit = dialogue.events[-1]
        assert isinstance(edit, Edit)
        assert edit.file_path == "store.go"
        assert edit.before == "m[k] = v"
        assert edit.after == "mu.Lock()\nm[k] = v"

    def test_preserves_order_across_speakers(self, tmp_path):
        """Interleaving is the evidence.

        An explanation that came *after* the developer pushed back means
        something different from one that came before it, and a rubric that
        cannot tell those apart will misattribute what they understood.
        """
        path = write_transcript(
            tmp_path,
            [
                human("add a cache"),
                assistant_text("Adding an LRU."),
                human("wait, why LRU and not TTL?"),
                assistant_text("TTL would evict hot keys."),
            ],
        )
        dialogue = extract_dialogue(path)

        assert [type(e) for e in dialogue.events] == [Turn, Reply, Turn, Reply]
        assert dialogue.events[2].text == "wait, why LRU and not TTL?"


class TestWhatIsStillExcluded:
    def test_still_drops_injected_skill_text(self, tmp_path):
        """Stage 0's load-bearing filter must survive the widening.

        Injected skill bodies are `type: user` with no `promptSource`. Reading
        more of the transcript is not a licence to read that.
        """
        path = write_transcript(
            tmp_path,
            [injected_skill("# Code Review Reception\n\nCore principle: ..."), human("ok go")],
        )
        dialogue = extract_dialogue(path)

        assert [e.text for e in dialogue.events if isinstance(e, Turn)] == ["ok go"]
        assert not any("Core principle" in getattr(e, "text", "") for e in dialogue.events)

    def test_still_drops_tool_results(self, tmp_path):
        """Tool output is the bulk of a transcript and none of the thinking."""
        path = write_transcript(
            tmp_path, [human("run the tests"), tool_result("42 passed in 0.05s")]
        )
        dialogue = extract_dialogue(path)

        assert [type(e) for e in dialogue.events] == [Turn]


class TestSize:
    def test_caps_a_single_runaway_edit(self, tmp_path):
        """One generated file must not crowd out the rest of the session.

        The cap is on each side of the edit rather than on the whole dialogue,
        so a 4000-line vendored blob costs its own slot and nobody else's.
        """
        path = write_transcript(
            tmp_path,
            [
                human("generate the client"),
                assistant_edit("client.py", "", "x = 1\n" * 20_000),
                human("why is it sync?"),
            ],
        )
        dialogue = extract_dialogue(path)

        edit = next(e for e in dialogue.events if isinstance(e, Edit))
        assert len(edit.after) < 20_000
        # The turn after the runaway edit is still there.
        assert dialogue.events[-1].text == "why is it sync?"
