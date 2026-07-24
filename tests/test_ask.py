"""Tests for the multiple-choice interrogation.

A scripted console stands in for the terminal; there is no model anywhere in
this path, so every branch runs at zero spend by construction rather than by
injection. What these pin down is the exits: correct pick, wrong pick, skip,
/wrong, invalid-then-valid input, and the one error that survives — a
malformed stored row.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from grask.ask import (
    ERROR,
    FAILED,
    PASSED,
    PREMISE_REJECTED,
    SKIPPED,
    AnswerTurn,
    PendingProbe,
    ask,
    grade,
)
from grask.probe import Rubric

RUBRIC = Rubric(
    topic="linking a PR to the issue it fixes",
    hypothesis="the developer accepted Refs #4923 without knowing what GitHub creates",
)

PENDING = PendingProbe(
    probe_id=7,
    question="What did swapping `GH#4923` for `Refs #4923` cause GitHub to create?",
    options=(
        "A closing keyword that closes #4923 on merge",
        "A cross-reference event on #4923's timeline",
        "A label linking the PR to the milestone",
    ),
    correct_idx=1,
    explanation="GitHub only autolinks `#N` when `#` follows a non-word character.",
    rubric=RUBRIC,
    created_at="2026-07-21T09:00:00+00:00",
)


class ScriptedConsole:
    """Replays a list of inputs and remembers everything it was shown."""

    def __init__(self, inputs: list[str]) -> None:
        self.inputs = list(inputs)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def show(self, text: str) -> None:
        self.shown.append(text)

    def prompt(self, text: str) -> str:
        self.prompts.append(text)
        if not self.inputs:
            raise AssertionError(f"console ran out of scripted input at: {text!r}")
        return self.inputs.pop(0)


class TestVerdicts:
    def test_the_correct_pick_passes(self):
        console = ScriptedConsole(["b"])

        result = ask(PENDING, console)

        assert result.outcome == PASSED
        assert len(result.turns) == 1
        assert result.turns[0].answer == PENDING.options[1]
        assert result.turns[0].question == PENDING.question

    def test_a_wrong_pick_fails(self):
        console = ScriptedConsole(["a"])

        result = ask(PENDING, console)

        assert result.outcome == FAILED
        assert result.turns[0].answer == PENDING.options[0]

    def test_the_pick_is_case_insensitive(self):
        result = ask(PENDING, ScriptedConsole(["B"]))

        assert result.outcome == PASSED

    def test_the_explanation_is_shown_on_a_pass_with_a_check(self):
        console = ScriptedConsole(["b"])

        ask(PENDING, console)

        assert any(
            text.startswith("✓") and PENDING.explanation in text for text in console.shown
        )

    def test_the_explanation_is_shown_on_a_fail_with_a_cross(self):
        console = ScriptedConsole(["c"])

        ask(PENDING, console)

        assert any(
            text.startswith("✗") and PENDING.explanation in text for text in console.shown
        )

    def test_nothing_is_spent(self):
        result = ask(PENDING, ScriptedConsole(["b"]))

        assert result.cost_usd == 0.0


class TestDisplay:
    def test_the_context_line_carries_the_topic(self):
        console = ScriptedConsole(["b"])

        ask(PENDING, console)

        assert any("linking a PR to the issue it fixes" in text for text in console.shown)

    def test_every_option_is_shown_with_its_letter(self):
        console = ScriptedConsole(["b"])

        ask(PENDING, console)

        listing = "\n".join(console.shown)
        for letter, option in zip("abc", PENDING.options):
            assert f"{letter}) {option}" in listing

    def test_the_pick_prompt_names_the_letter_range(self):
        console = ScriptedConsole(["b"])

        ask(PENDING, console)

        assert any("[a-c]" in p for p in console.prompts)


class TestSkip:
    def test_skip_at_the_pick_prompt(self):
        result = ask(PENDING, ScriptedConsole([""]))

        assert result.outcome == SKIPPED
        assert result.confidence is None
        assert result.turns == ()


class TestWrong:
    def test_wrong_with_an_objection(self):
        console = ScriptedConsole(["/wrong", "I never used Refs, that was the agent"])

        result = ask(PENDING, console)

        assert result.outcome == PREMISE_REJECTED
        assert result.objection == "I never used Refs, that was the agent"

    def test_wrong_without_an_objection(self):
        result = ask(PENDING, ScriptedConsole(["/wrong", ""]))

        assert result.outcome == PREMISE_REJECTED
        assert result.objection is None


class TestInvalidInput:
    def test_the_pick_reprompts_with_a_hint_on_an_unknown_letter(self):
        console = ScriptedConsole(["z", "b"])

        result = ask(PENDING, console)

        assert result.outcome == PASSED
        assert any("a-c" in text for text in console.shown)

    def test_the_pick_reprompts_on_a_multi_character_answer(self):
        result = ask(PENDING, ScriptedConsole(["ab", "b"]))

        assert result.outcome == PASSED


class TestMalformed:
    """The one place `error` survives: a stored row the ask cannot serve."""

    @pytest.mark.parametrize(
        "broken",
        [
            {"options": ()},
            {"correct_idx": None},
            {"correct_idx": 9},
            {"explanation": "  "},
        ],
    )
    def test_a_malformed_row_is_an_error_before_any_prompt(self, broken):
        console = ScriptedConsole([])

        result = ask(replace(PENDING, **broken), console)

        assert result.outcome == ERROR
        assert console.prompts == []


class TestGrade:
    """The pure (pending, pick) -> Interrogation map the record path uses."""

    def test_the_correct_pick_passes(self):
        result = grade(PENDING, "b")

        assert result.outcome == PASSED
        assert result.probe_id == PENDING.probe_id
        assert result.turns == (
            AnswerTurn(turn=0, question=PENDING.question, answer=PENDING.options[1]),
        )
        assert result.cost_usd == 0.0

    def test_a_wrong_pick_fails(self):
        result = grade(PENDING, "a")

        assert result.outcome == FAILED
        assert result.turns[0].answer == PENDING.options[0]

    def test_the_pick_is_case_insensitive(self):
        assert grade(PENDING, "B").outcome == PASSED

    def test_a_letter_beyond_the_stored_options_is_rejected(self):
        with pytest.raises(ValueError):
            grade(PENDING, "d")  # PENDING has three options: a-c

    def test_a_multi_character_pick_is_rejected(self):
        with pytest.raises(ValueError):
            grade(PENDING, "ab")
