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
    MARKDOWN,
    PASSED,
    PLAIN,
    PREMISE_REJECTED,
    SKIPPED,
    AnswerTurn,
    PendingProbe,
    ask,
    grade,
    resolution,
    result_block,
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
        assert result.turns == ()

    def test_skipping_still_shows_the_answer(self):
        """Skipping used to print nothing at all. It is usually "I don't know",
        and `UNIQUE(probe_id)` means the probe will not come back, so silence
        just threw the payoff away."""
        console = ScriptedConsole([""])

        ask(PENDING, console)

        assert any(PENDING.explanation in text for text in console.shown)
        assert any(PENDING.options[1] in text for text in console.shown)


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


class TestResultBlock:
    """The one renderer. Both surfaces print what this returns and nothing else,
    which is the property that stops them drifting apart — the `/grask` skill
    once printed a bare `\u2717` on a line of its own while the terminal printed a
    verdict and an explanation together."""

    def block(self, pick: str, style: str) -> str:
        return result_block(PENDING, grade(PENDING, pick), style=style)

    @pytest.mark.parametrize("style", [PLAIN, MARKDOWN])
    def test_a_pass_says_correct_in_words(self, style):
        """A glyph alone can be missed entirely; that is the whole complaint."""
        block = self.block("b", style)

        assert "Correct" in block
        assert "\u2713" in block
        assert PENDING.explanation in block

    @pytest.mark.parametrize("style", [PLAIN, MARKDOWN])
    def test_a_pass_does_not_echo_the_pick(self, style):
        """You got it right. Repeating the option back is noise."""
        block = self.block("b", style)

        assert "You picked" not in block

    @pytest.mark.parametrize("style", [PLAIN, MARKDOWN])
    def test_a_fail_names_the_correct_letter(self, style):
        block = self.block("a", style)

        assert "Incorrect" in block
        assert "\u2717" in block
        assert "b)" in block
        assert PENDING.explanation in block

    def test_a_fail_spells_both_options_out_in_markdown(self):
        """The picker is gone by the time this renders, so the letters alone
        would be unreadable."""
        block = self.block("a", MARKDOWN)

        assert PENDING.options[0] in block
        assert PENDING.options[1] in block

    def test_a_fail_names_letters_only_for_the_pick_in_plain(self):
        """The terminal still has the options on screen above the prompt, so the
        wrong option is named by letter and not reprinted in full."""
        block = self.block("a", PLAIN)

        assert "you picked a" in block
        assert PENDING.options[0] not in block
        assert PENDING.options[1] in block

    @pytest.mark.parametrize("style", [PLAIN, MARKDOWN])
    def test_a_skip_gets_the_answer_but_no_verdict(self, style):
        block = result_block(PENDING, resolution(PENDING, SKIPPED), style=style)

        assert "Skipped" in block
        assert PENDING.options[1] in block
        assert PENDING.explanation in block
        assert "Incorrect" not in block and "Correct" not in block

    @pytest.mark.parametrize("style", [PLAIN, MARKDOWN])
    def test_a_rejected_premise_gets_no_key_and_no_explanation(self, style):
        """They said the question is wrong. Answering with its own answer key
        argues past them."""
        block = result_block(PENDING, resolution(PENDING, PREMISE_REJECTED), style=style)

        assert "Premise rejected" in block
        assert PENDING.options[1] not in block
        assert PENDING.explanation not in block

    def test_only_markdown_carries_the_topic(self):
        """The terminal printed it above the question already (`context_line`);
        the skill withheld it until the answer was settled."""
        assert PENDING.rubric.topic in self.block("a", MARKDOWN)
        assert PENDING.rubric.topic not in self.block("a", PLAIN)

    @pytest.mark.parametrize("style", [PLAIN, MARKDOWN])
    def test_a_row_with_no_key_says_nothing_it_cannot_support(self, style):
        """`unservable` normally catches these first. If one reaches here, the
        verdict line is all it may claim."""
        broken = replace(PENDING, correct_idx=None)

        block = result_block(broken, resolution(broken, FAILED), style=style)

        assert PENDING.explanation not in block
        assert PENDING.options[1] not in block
