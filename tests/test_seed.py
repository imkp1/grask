"""Tests for stage 2, the seed.

No LLM is called here. What these pin down is the part of stage 2 that is not a
prompt: the hypothesis is mandatory, and quotes are verified rather than
requested.

The evidence rule is inherited from stage 1 and for the same reason. Telling a
model to quote verbatim is not a control; checking that the quote appears in the
transcript is. A hallucinated quote is worse here than in triage — triage uses it
to rank, stage 3 reads it back to the developer, and being misquoted to your face
is the fastest way to conclude the tool is guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grask.dialogue import Dialogue, Reply
from grask.llm import Completion, LLMError
from grask.seed import build_prompt, parse_seed
from grask.transcript import Turn
from grask.triage import Moment


def dialogue(*texts: str) -> Dialogue:
    events: list = []
    for i, text in enumerate(texts):
        kind = Turn(text=text, timestamp=None, index=i) if i % 2 == 0 else Reply(text=text, index=i)
        events.append(kind)
    return Dialogue(
        session_id="0198e4f1",
        path=Path("/tmp/0198e4f1.jsonl"),
        events=events,
    )


def moment(topic: str = "idempotency of the retry path", turn: int = 0) -> Moment:
    return Moment(
        turn=turn,
        signal="asked_why",
        topic=topic,
        quote="why do we need an idempotency key here?",
        shows="asked why rather than accepting the retry wrapper",
    )


def completion(text: str) -> Completion:
    return Completion(text=text, cost_usd=0.01, duration_ms=900)


DIALOGUE = dialogue(
    "why do we need an idempotency key here?",
    "Because a retried POST would charge the customer twice.",
    "ok, add it",
)


def response(**overrides) -> str:
    body = {
        "topic": "idempotency of the retry path",
        "quotes": ["why do we need an idempotency key here?"],
        "refs": ["billing.py:41"],
        "decision": "added an idempotency key derived from the order id",
        "hypothesis": (
            "The developer accepted that a key prevents double charges without "
            "knowing the key must be stable across retries to do so."
        ),
    }
    body.update(overrides)
    return json.dumps(body)


class TestHypothesisIsMandatory:
    def test_rejects_a_seed_with_no_hypothesis(self):
        """The hypothesis is the seed's reason to exist.

        Stage 3 derives the rubric from it and a "not worth asking" vote is
        attributed to it. A seed without one produces a question nobody can
        diagnose, which is worse than no question.
        """
        with pytest.raises(LLMError):
            parse_seed(DIALOGUE, moment(), completion(response(hypothesis="")))

    def test_rejects_a_hypothesis_that_is_not_falsifiable(self):
        """A noun phrase is a topic, not a claim.

        The design calls this out by name: `concern` failed as a signal because
        it named a subject rather than asserting anything. A hypothesis that
        cannot be wrong cannot be tested by a probe.
        """
        with pytest.raises(LLMError):
            parse_seed(DIALOGUE, moment(), completion(response(hypothesis="idempotency keys")))


class TestQuotesAreVerified:
    def test_drops_a_quote_that_is_not_in_the_transcript(self):
        """The control is verification, not instruction.

        A fabricated quote read back to the developer is the failure that ends
        trust in one sentence.
        """
        parsed = parse_seed(
            DIALOGUE,
            moment(),
            completion(
                response(
                    quotes=[
                        "why do we need an idempotency key here?",
                        "I think we should use a UUID",
                    ]
                )
            ),
        )

        assert parsed.quotes == ("why do we need an idempotency key here?",)

    def test_rejects_a_seed_where_no_quote_survives(self):
        """Nothing verifiable means nothing to ask about.

        Silence is a valid outcome — a seed grounded in nothing the developer
        said is exactly the generic question the design says gets grask
        disabled in week two.
        """
        with pytest.raises(LLMError):
            parse_seed(DIALOGUE, moment(), completion(response(quotes=["let's ship it"])))

    def test_matches_a_quote_ignoring_surrounding_whitespace(self):
        """Verification must not be so literal it rejects true quotes.

        Models re-wrap text. Failing a genuine quote over a newline would push
        us toward trusting the model instead, which is the wrong direction.
        """
        parsed = parse_seed(
            DIALOGUE,
            moment(),
            completion(response(quotes=["  why do we need an idempotency key here?\n"])),
        )

        assert len(parsed.quotes) == 1


class TestPrompt:
    def test_gives_the_model_the_agent_side_of_the_conversation(self):
        """Stage 2 sees what stage 1 could not.

        The explanation the developer accepted is in the assistant's turns. A
        seed built only from what the developer typed cannot say what they
        accepted, only that they accepted something.
        """
        prompt = build_prompt(DIALOGUE, moment())

        assert "Because a retried POST would charge the customer twice." in prompt

    def test_anchors_on_the_triaged_moment(self):
        """Stage 2 does not re-decide what stage 1 already decided.

        Selection is deterministic and already happened. Letting stage 2 wander
        to a different moment would reintroduce the run-to-run topic drift that
        moving selection out of the model was meant to fix.
        """
        prompt = build_prompt(DIALOGUE, moment(topic="idempotency of the retry path"))

        assert "idempotency of the retry path" in prompt


def test_the_prompt_prefers_technical_mechanisms():
    """Prompt-only steering from the 2026-07-22 design: the hypothesis names a
    mechanism, not a process reading of the moment."""
    from grask.seed import PROMPT

    assert "technical mechanism" in PROMPT
