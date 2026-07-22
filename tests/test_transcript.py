"""Tests for stage 0 extraction.

The filter these pin down is load-bearing: if it lets injected skill text through,
every downstream stage generates questions about text the developer never wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

from grask.transcript import Turn, extract, find_transcripts


def write_transcript(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "0198e4f1-0000-0000-0000-000000000000.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def human(text: str, ts: str = "2026-07-20T00:41:13.482Z", source: str = "typed") -> dict:
    return {
        "type": "user",
        "promptSource": source,
        "timestamp": ts,
        "cwd": "/Users/dev/projects/example",
        "gitBranch": "main",
        "message": {"role": "user", "content": text},
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
    """A skill/hook injection. Looks like a user record but nobody typed it."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def assistant_edit(file_path: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Editing that now."},
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "Edit",
                    "input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
                },
            ],
        },
    }


class TestHumanTurnFilter:
    def test_keeps_typed_turns(self, tmp_path):
        path = write_transcript(tmp_path, [human("why does this need a mutex?")])
        session = extract(path)
        assert [t.text for t in session.turns] == ["why does this need a mutex?"]

    def test_drops_tool_results(self, tmp_path):
        path = write_transcript(tmp_path, [human("run the tests"), tool_result("42 passed")])
        session = extract(path)
        assert [t.text for t in session.turns] == ["run the tests"]

    def test_drops_injected_skill_text(self, tmp_path):
        """The regression that matters: injected text is not the developer.

        These records have type=user and no toolUseResult, so anything keying on
        those two fields alone would treat a skill body as a human turn.
        """
        path = write_transcript(
            tmp_path,
            [injected_skill("# Code Review Reception\n\nCore principle: ..."), human("ok go")],
        )
        session = extract(path)
        assert [t.text for t in session.turns] == ["ok go"]

    def test_keeps_accepted_suggestions_and_queued(self, tmp_path):
        """Regression: these are the developer too.

        An early version accepted only promptSource="typed", silently discarding
        18% of all human turns — and disproportionately the interrogative ones,
        which are precisely what grask selects on.
        """
        path = write_transcript(
            tmp_path,
            [
                human(
                    "can we mix adaptive conversation with scenarios?",
                    source="suggestion_accepted",
                ),
                human("delete the old plan", source="queued"),
            ],
        )
        assert len(extract(path).turns) == 2

    def test_drops_non_human_prompt_sources(self, tmp_path):
        path = write_transcript(
            tmp_path,
            [human("programmatic", source="sdk"), human("harness", source="system"), human("mine")],
        )
        assert [t.text for t in extract(path).turns] == ["mine"]

    def test_drops_empty_and_whitespace_turns(self, tmp_path):
        path = write_transcript(tmp_path, [human("   "), human(""), human("real")])
        session = extract(path)
        assert [t.text for t in session.turns] == ["real"]


class TestResilience:
    def test_skips_malformed_lines(self, tmp_path):
        """Live transcripts can end mid-write; a partial line must not lose the session."""
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(human("first")) + "\n" + '{"type": "user", "prompt' + "\n",
            encoding="utf-8",
        )
        session = extract(path)
        assert [t.text for t in session.turns] == ["first"]

    def test_survives_missing_message_field(self, tmp_path):
        path = write_transcript(tmp_path, [{"type": "user", "promptSource": "typed"}, human("ok")])
        assert [t.text for t in extract(path).turns] == ["ok"]

    def test_empty_transcript(self, tmp_path):
        path = write_transcript(tmp_path, [])
        session = extract(path)
        assert session.turns == [] and session.extracted_bytes == 0


class TestMetadata:
    def test_captures_cwd_and_branch(self, tmp_path):
        session = extract(write_transcript(tmp_path, [human("hi")]))
        assert session.cwd == "/Users/dev/projects/example"
        assert session.git_branch == "main"

    def test_collects_edited_files(self, tmp_path):
        path = write_transcript(
            tmp_path,
            [human("fix it"), assistant_edit("/repo/pay.py"), assistant_edit("/repo/pay.py")],
        )
        assert extract(path).files_touched == {"/repo/pay.py"}

    def test_turns_are_indexed_in_order(self, tmp_path):
        path = write_transcript(tmp_path, [human("one"), tool_result("x"), human("two")])
        assert [t.index for t in extract(path).turns] == [0, 1]

    def test_parses_timestamp(self, tmp_path):
        session = extract(write_transcript(tmp_path, [human("hi")]))
        assert session.turns[0].timestamp is not None
        assert session.turns[0].timestamp.year == 2026

    def test_bad_timestamp_is_none_not_fatal(self, tmp_path):
        session = extract(write_transcript(tmp_path, [human("hi", ts="not-a-date")]))
        assert session.turns[0].timestamp is None


class TestQuestionHeuristic:
    """Crude by design — it ranks candidates, it does not decide anything."""

    def test_detects_question_forms(self):
        for text in [
            "why does this work",
            "what if it fails",
            "is that safe?",
            "how does it retry",
        ]:
            assert Turn(text, None, 0).is_question, text

    def test_ignores_plain_directives(self):
        for text in ["add a retry wrapper", "commit this", "run the tests"]:
            assert not Turn(text, None, 0).is_question, text


class TestDiscovery:
    def test_missing_root_returns_empty(self, tmp_path):
        assert find_transcripts(tmp_path / "nope") == []

    def test_finds_nested_transcripts(self, tmp_path):
        (tmp_path / "proj-a").mkdir()
        (tmp_path / "proj-a" / "s1.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "proj-b").mkdir()
        (tmp_path / "proj-b" / "s2.jsonl").write_text("", encoding="utf-8")
        assert len(find_transcripts(tmp_path)) == 2

    def test_excludes_subagent_transcripts(self, tmp_path):
        """A subagent never speaks to the developer, so its work is not theirs to defend."""
        (tmp_path / "proj").mkdir()
        (tmp_path / "proj" / "session.jsonl").write_text("", encoding="utf-8")
        subagents = tmp_path / "proj" / "session-id" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-abc.jsonl").write_text("", encoding="utf-8")

        found = find_transcripts(tmp_path)
        assert [p.name for p in found] == ["session.jsonl"]
