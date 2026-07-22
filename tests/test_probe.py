"""Tests for stage 3, the question and its answer key.

No LLM is called here. These pin down the structural gates the design states
outright: one question, 3-4 pairwise-distinct options with exactly one correct,
a valid index, a non-empty explanation, and a shuffle that moves positions
without ever moving the truth.

The shuffle property matters more than it looks: `correct_idx` is stored and the
verdict at ask time is a mechanical comparison against it, so a shuffle that
lost track of the correct option would grade every future pick against noise.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from grask.dialogue import Dialogue, Reply
from grask.llm import Completion, LLMError
from grask.probe import (
    MAX_ATTEMPTS,
    Rubric,
    build_prompt,
    parse_probe,
    probe,
)
from grask.seed import Seed
from grask.transcript import Turn

SEED = Seed(
    session_id="0198e4f1",
    turn=0,
    signal="asked_why",
    topic="linking a PR to the issue it fixes",
    quotes=("why did GH#4923 not link anything?",),
    refs=("PULL_REQUEST.md:3",),
    decision="swapped GH#4923 for Refs #4923 in the PR body",
    hypothesis=(
        "The developer accepted that Refs #4923 links the PR without knowing "
        "GitHub only autolinks #N after a non-word character."
    ),
)

DIALOGUE = Dialogue(
    session_id="0198e4f1",
    path=Path("/tmp/0198e4f1.jsonl"),
    events=[
        Turn(text="why did GH#4923 not link anything?", timestamp=None, index=0),
        Reply(text="Because GitHub only autolinks #N after a non-word character.", index=1),
    ],
)

CORRECT_OPTION = "A cross-reference event on the issue's timeline"


def completion(text: str) -> Completion:
    return Completion(text=text, cost_usd=0.01, duration_ms=900)


def response(**overrides) -> str:
    body = {
        "question": "What does GitHub create when `#4923` follows a non-word character?",
        "options": [
            "A closing keyword that closes the issue on merge",
            CORRECT_OPTION,
            "A label linking the PR to the milestone",
        ],
        "correct": 1,
        "explanation": (
            "GitHub only autolinks `#N` after a non-word character, producing a "
            "cross-reference event."
        ),
    }
    body.update(overrides)
    return json.dumps(body)


class TestChoices:
    def test_accepts_a_well_formed_probe(self):
        parsed = parse_probe(SEED, completion(response()))

        assert len(parsed.options) == 3
        assert parsed.options[parsed.correct_idx] == CORRECT_OPTION
        assert parsed.explanation.startswith("GitHub only autolinks")

    def test_rejects_fewer_than_three_options(self):
        """Two options is a coin flip, and a coin flip cannot measure anything."""
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(options=["one", "two"], correct=0)))

    def test_rejects_more_than_four_options(self):
        """Claude's question UI takes at most 4 options, so 5 is unservable there."""
        with pytest.raises(LLMError):
            parse_probe(
                SEED,
                completion(response(options=[f"option {n}" for n in range(5)], correct=0)),
            )

    def test_rejects_duplicate_options_after_strip(self):
        """Two identical options make the answer key ambiguous by position."""
        with pytest.raises(LLMError):
            parse_probe(
                SEED, completion(response(options=["same", "  same  ", "other"], correct=0))
            )

    def test_rejects_an_out_of_range_correct_index(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(correct=3)))

    def test_rejects_a_boolean_correct(self):
        """`True` is an int in Python; an answer key of `true` is a parser bug waiting."""
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(correct=True)))

    def test_rejects_an_empty_explanation(self):
        """The explanation is the payoff shown after the pick; without it a wrong
        pick teaches nothing and the whole encounter was friction."""
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(explanation="  ")))

    def test_rejects_a_missing_question(self):
        with pytest.raises(LLMError):
            parse_probe(SEED, completion(response(question="   ")))

    def test_carries_the_hypothesis_into_the_rubric(self):
        """A wrong pick must stay attributable to the hypothesis that provoked it."""
        parsed = parse_probe(SEED, completion(response()))

        assert isinstance(parsed.rubric, Rubric)
        assert parsed.rubric.hypothesis == SEED.hypothesis
        assert parsed.rubric.topic == SEED.topic


class TestShuffle:
    def test_correct_idx_always_names_the_option_the_model_marked_correct(self):
        """The property the spec states: shuffle moves positions, never truth."""
        for seed_value in range(50):
            parsed = parse_probe(
                SEED, completion(response()), rng=random.Random(seed_value)
            )

            assert parsed.options[parsed.correct_idx] == CORRECT_OPTION

    def test_the_shuffle_actually_shuffles(self):
        """Storage-time shuffling is the defence against the model's positional
        bias; an identity shuffle would leave `correct` wherever the model
        habitually puts it."""
        orders = {
            parse_probe(SEED, completion(response()), rng=random.Random(s)).options
            for s in range(20)
        }

        assert len(orders) > 1


class TestOneQuestion:
    def test_rejects_a_multi_part_question(self):
        """The price of an encounter is one question, ~20 seconds.

        Two questions stapled together is a quiz, and a quiz is a destination —
        the thing the whole trigger-based design exists to avoid.
        """
        with pytest.raises(LLMError):
            parse_probe(
                SEED,
                completion(
                    response(
                        question=(
                            "What does the autolink create? And how would you test that?"
                        )
                    )
                ),
            )

    def test_rejects_two_questions_joined_by_a_conjunction(self):
        """The failure the question-mark count misses, observed in a real run:
        one question mark, two questions."""
        with pytest.raises(LLMError):
            parse_probe(
                SEED,
                completion(
                    response(
                        question=(
                            "What has to be true of the reference for GitHub to link it, "
                            "and how would you find out if it stopped being true?"
                        )
                    )
                ),
            )

    def test_allows_a_conjunction_that_is_not_a_second_question(self):
        """Do not overcorrect into rejecting ordinary English.

        "and" inside a clause is not a second question, and a check that
        rejects it would push stage 3 toward stilted phrasing to satisfy a
        parser rather than a developer.
        """
        parsed = parse_probe(
            SEED,
            completion(
                response(
                    question=(
                        "What does GitHub create when the reference is `Refs #4923` "
                        "and the PR body is saved?"
                    )
                )
            ),
        )

        assert parsed.question.endswith("?")


class TestRegeneration:
    """A rejected probe costs one more call, not the whole session."""

    COMPOUND = (
        "What has to be true of the reference for GitHub to link it, "
        "and how would you find out if it stopped being true?"
    )

    def test_a_rejected_question_is_regenerated(self):
        calls = []

        def fake(prompt: str) -> Completion:
            calls.append(prompt)
            if len(calls) == 1:
                return completion(response(question=self.COMPOUND))
            return completion(response())

        parsed = probe(SEED, DIALOGUE, complete=fake)

        assert len(calls) == 2
        assert parsed.question.startswith("What does GitHub create")

    def test_bad_options_are_also_regenerated(self):
        """The new gates ride the same retry the compound-stem gate always had."""
        calls = []

        def fake(prompt: str) -> Completion:
            calls.append(prompt)
            if len(calls) == 1:
                return completion(response(options=["one", "two"], correct=0))
            return completion(response())

        parsed = probe(SEED, DIALOGUE, complete=fake)

        assert len(calls) == 2
        assert len(parsed.options) == 3

    def test_the_retry_is_told_what_it_got_wrong(self):
        """A blind retry resamples the same distribution.

        The rejected question goes back in, because "do not do that again" is
        only actionable if `that` is on the page.
        """
        calls = []

        def fake(prompt: str) -> Completion:
            calls.append(prompt)
            if len(calls) == 1:
                return completion(response(question=self.COMPOUND))
            return completion(response())

        probe(SEED, DIALOGUE, complete=fake)

        assert self.COMPOUND not in calls[0], "the first call must be the plain prompt"
        assert self.COMPOUND in calls[1], "the retry must show the model what was rejected"
        assert "rejected" in calls[1].lower()

    def test_gives_up_after_the_attempt_budget(self):
        """Regeneration is bounded. An unbounded retry against a model that
        keeps producing the same shape is an unbounded bill.
        """
        calls = []

        def fake(prompt: str) -> Completion:
            calls.append(prompt)
            return completion(response(question=self.COMPOUND))

        with pytest.raises(LLMError):
            probe(SEED, DIALOGUE, complete=fake)

        assert len(calls) == MAX_ATTEMPTS

    def test_a_transient_call_failure_is_also_retried(self):
        """Observed on a real run: `claude exited 1` with empty stderr, once,
        with the CLI healthy immediately after. One retry covers it.
        """
        calls = []

        def fake(prompt: str) -> Completion:
            calls.append(prompt)
            if len(calls) == 1:
                raise LLMError("claude exited 1: ")
            return completion(response())

        parsed = probe(SEED, DIALOGUE, complete=fake)

        assert len(calls) == 2
        assert parsed.question.startswith("What does GitHub create")

    def test_cost_includes_the_attempts_that_failed(self):
        """A rejected attempt is spent money. Reporting only the winning call
        would make stage 3 look cheaper than it is, and the per-session cost is
        the number this whole design is judged on.
        """

        def fake(prompt: str) -> Completion:
            if "rejected" not in prompt.lower():
                return completion(response(question=self.COMPOUND))
            return completion(response())

        parsed = probe(SEED, DIALOGUE, complete=fake)

        assert parsed.cost_usd == pytest.approx(0.02)


class TestPrompt:
    def test_gives_the_model_the_hypothesis_to_test(self):
        prompt = build_prompt(SEED, DIALOGUE)

        assert SEED.hypothesis in prompt

    def test_reads_the_transcript_not_only_the_seed(self):
        """Stage 3 reads the full transcript by design.

        A compressed seed can name the topic but cannot name the artifact that
        shipped, and compressing here would cap probe quality to save tokens on
        the cheapest stage by input size.
        """
        prompt = build_prompt(SEED, DIALOGUE)

        assert "Because GitHub only autolinks #N after a non-word character." in prompt

    def test_forbids_conversational_narrative(self):
        """The stem asks about the mechanism, never retells the conversation."""
        prompt = build_prompt(SEED, DIALOGUE)

        assert "you asked" in prompt
        assert "mechanism" in prompt
