"""Stage 3: one multiple-choice question about the mechanism that shipped.

Reads the full transcript rather than the seed alone. The seed is enough to name
the topic; it is not enough to name the file, flag, or identifier that actually
shipped, and a question that cannot do that is a generic question — the kind the
design says gets grill disabled in week two.

The answer key is minted here, before the developer exists to the question.
Picking an option IS the answer: the verdict is `pick == correct_idx`, decided
mechanically at ask time, so everything that makes the question fair — one
correct option, plausible wrong mechanisms as distractors, an explanation worth
reading either way — has to be enforced at generation time or never.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, replace
from typing import Any

from grill.dialogue import Dialogue
from grill.llm import Completion, LLMError, complete, extract_json_object
from grill.seed import Seed, render_dialogue

PROBE_KEYS = ("question", "options", "correct", "explanation")

MIN_OPTIONS = 3
MAX_OPTIONS = 4  # Claude's native question UI takes at most 4 options.

# One question, ~20 seconds. A second question stapled on turns an encounter into
# a quiz, and a quiz is a destination — the thing the whole trigger-based design
# exists to avoid.
MAX_QUESTION_MARKS = 1

# Counting question marks catches "…? And how…?" and misses the shape stage 3
# actually produces, observed on the first real run: "what has to be true of that
# payload …, and how would you find out if it stopped being true?" — one mark,
# two questions, about a minute of work.
#
# The comma is doing real work here. Requiring it keeps ordinary subordinate
# clauses ("… and the request were replayed?") out of the net, at the cost of
# missing a comma-less second question. That is the right direction to be wrong:
# a false reject costs one regenerated probe, a false accept costs the developer
# their twenty-second promise. The prompt remains the primary control; this is
# the check that makes it true rather than likely.
SECOND_QUESTION = re.compile(
    r",\s*(?:and|also|plus|then)\s+"
    r"(?:how|what|why|when|where|who|which|whether|can|could|would|should|do|does|did|is|are|will)"
    r"\b",
    flags=re.IGNORECASE,
)

# The number of times stage 3 is allowed to produce an unusable probe before the
# session is written off. Three because the observed failure is stochastic — two
# real runs on the same seed produced two different compound questions — and
# because an unbounded retry against a model stuck in one shape is an unbounded
# bill. The comment above states the cost of a false reject as "one regenerated
# probe"; this is what makes that sentence true.
MAX_ATTEMPTS = 3

# Appended verbatim after a structural rejection. Naming the rejected question is
# the point: every rule below is already in the prompt and was already ignored,
# so the retry has to show the model the thing it did rather than restate the
# rule it broke.
CORRECTION = """\

## Your previous attempt was rejected

You returned this question:

{question}

It was rejected: {reason}

Fix exactly what the rejection names, changing nothing else about your
approach. The rules: one question (a second clause after a comma — "…, and how
would you…" — is a second question; drop it entirely), 3 to 4 one-line options
with exactly one correct, no duplicates, `correct` a valid zero-based index,
and a non-empty `explanation`.

Return the same JSON shape.
"""


class ProbeRejected(LLMError):
    """A structurally unusable probe, carrying the question that failed.

    A plain `LLMError` says a call went wrong; this says the model answered and
    the answer broke a rule. Only the second kind is worth showing back to the
    model, which is why it is a separate type rather than a message prefix.
    """

    def __init__(self, reason: str, question: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.question = question


@dataclass(frozen=True)
class Rubric:
    """The claim a probe tests, and the frame it was asked in.

    `hypothesis` is what makes a failure diagnosable: a wrong pick is
    attributable to a wrong hypothesis or to a right hypothesis asked badly.
    Without it a failed probe is an unactionable complaint.
    """

    topic: str
    hypothesis: str


@dataclass(frozen=True)
class Probe:
    """One multiple-choice question: stem, shuffled options, and the answer key."""

    question: str
    options: tuple[str, ...]
    correct_idx: int
    explanation: str
    rubric: Rubric
    cost_usd: float | None = None
    duration_ms: int | None = None


PROMPT = """\
You are stage 3 of `grill`. Everything has already been decided except the
question itself: the session was triaged, the moment selected, and a hypothesis
formed about what this developer accepted without fully understanding.

Produce ONE multiple-choice question that tests the mechanism at the core of
that hypothesis.

## The hypothesis you are testing

{hypothesis}

## The topic

{topic}

## What shipped

{decision}

## What the developer said

{quotes}

## The session

{rendered}

## What to produce

`question` — ONE question about the mechanism itself: what the API, tool,
configuration, or algorithm does or requires. Never a retelling of the
conversation: no "you asked", "you said", "Claude did", or any conversational
narrative. Ground it in the session's actual artifact — name the file, flag, or
identifier that shipped — but ask how the mechanism works, not what happened.
It must be one question: no "and", no "also", no parts.

The artifact names the setting; the mechanism must outlive it. Ask about
something still true away from this repository — what the API returns, what
the shell cannot do, what the tool guarantees. A question whose answer is
"because this file says so" — the contents of a local script, the wording of a
local spec, what a step of a local design does — can only be answered by
whoever sat through the session, and teaches nothing the moment they close the
file. Apply the test before writing: would a competent developer who never saw
this session be better at their work for knowing the answer? If not, there is
no probe here, however specific the detail.

`options` — {min_options} to {max_options} one-line answers, exactly ONE of
them correct. Every wrong option must describe a plausible wrong MECHANISM —
something a developer who half-understood the decision would believe. The
dangerous failure is a fluent answer describing a different mechanism, and the
distractors are where you catch it. No "all of the above" or "none of the
above". No option may be a joke or an obvious throwaway.

Each option asserts ONE mechanism. An option that couples two claims with
"and" — a limit AND a transformation, a rule AND its consequence — is
unusable even as the correct one: whoever picks it cannot tell which half was
graded, and the unchecked half is where a falsehood survives. Split it, or
drop the second claim. Assert only what this session or the named artifact
actually shows; a mechanism you are recalling rather than reading does not go
in.

`correct` — the zero-based index of the correct option, as you listed them.

`explanation` — 1 to 3 sentences shown after the developer picks, right or
wrong: why the correct option is correct, in terms of the mechanism.

State the mechanism and stop. Do not extend it into a downstream consequence:
the clause after "so", "which means", or "that's why" is where this stage is
wrong most often — a true mechanism carries an invented result, and the
developer who answered correctly still leaves with the falsehood. If the
consequence is worth testing, it belongs in the options as a distractor, not
asserted as fact after the pick.

Do not be clever or arch. A developer who has been coding for six hours reads
this as a few lines of text, and a question that performs is a question that
gets skipped.

## Respond

One JSON object, nothing else:

{{"question": "...", "options": ["...", "..."], "correct": 0, "explanation": "..."}}
"""


def build_prompt(seed: Seed, dialogue: Dialogue) -> str:
    return PROMPT.format(
        hypothesis=seed.hypothesis,
        topic=seed.topic,
        decision=seed.decision or "(not recorded)",
        quotes="\n".join(f"- {q}" for q in seed.quotes),
        rendered=render_dialogue(dialogue),
        min_options=MIN_OPTIONS,
        max_options=MAX_OPTIONS,
    )


def reject_if_compound(question: str) -> None:
    """The one-question rule.

    A multiple-choice stem with two questions cannot have one correct option,
    so this gate is what keeps the mechanical verdict meaningful.
    """
    if question.count("?") > MAX_QUESTION_MARKS:
        raise ProbeRejected(f"probe asks more than one question: {question!r}", question)

    if SECOND_QUESTION.search(question):
        raise ProbeRejected(
            f"probe asks a second question after a conjunction: {question!r}", question
        )


def validate_choices(parsed: dict[str, Any], question: str) -> tuple[tuple[str, ...], int, str]:
    """The option gates: shape, count, and a correct index that points at a real option."""
    raw = parsed.get("options")
    options = (
        tuple(o.strip() for o in raw if isinstance(o, str) and o.strip())
        if isinstance(raw, list)
        else ()
    )
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ProbeRejected(
            f"probe needs {MIN_OPTIONS}-{MAX_OPTIONS} options, got {len(options)}",
            question,
        )
    if len(set(options)) != len(options):
        raise ProbeRejected("probe has duplicate options", question)

    correct = parsed.get("correct")
    if isinstance(correct, bool) or not isinstance(correct, int) or not 0 <= correct < len(options):
        raise ProbeRejected(f"'correct' is not a valid option index: {correct!r}", question)

    explanation = parsed.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ProbeRejected("probe has no explanation", question)

    return options, correct, explanation.strip()


def shuffle_choices(
    options: tuple[str, ...], correct: int, rng: random.Random | None = None
) -> tuple[tuple[str, ...], int]:
    """Shuffle at storage time, not display time.

    The stored row is the single source of truth for what position was shown,
    so `correct_idx` must be minted post-shuffle, here, once.
    """
    order = list(range(len(options)))
    (rng or random).shuffle(order)
    return tuple(options[i] for i in order), order.index(correct)


def parse_probe(
    seed: Seed, completion: Completion, *, rng: random.Random | None = None
) -> Probe:
    """Read a probe, enforcing the structural gates the design states as gates.

    All rejections are structural rather than qualitative. Whether a question is
    *good* is settled by the yes-rate, not here; whether it is one mechanically
    gradable multiple-choice question is settled here, because a no on either
    makes the yes-rate uninterpretable.
    """
    parsed = extract_json_object(completion.text, salvage_keys=PROBE_KEYS)

    question = parsed.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ProbeRejected("probe has no question")
    question = question.strip()

    reject_if_compound(question)

    options, correct, explanation = validate_choices(parsed, question)
    shuffled, correct_idx = shuffle_choices(options, correct, rng)

    return Probe(
        question=question,
        options=shuffled,
        correct_idx=correct_idx,
        explanation=explanation,
        rubric=Rubric(topic=seed.topic, hypothesis=seed.hypothesis),
        cost_usd=completion.cost_usd,
        duration_ms=completion.duration_ms,
    )


def probe(
    seed: Seed,
    dialogue: Dialogue,
    *,
    complete=complete,
    attempts: int = MAX_ATTEMPTS,
) -> Probe:
    """Derive the question and its answer key from a stored seed and a live transcript.

    Retries a rejected probe rather than failing the session. The gates in
    `parse_probe` reject a *stochastic* output — the same seed produced two
    different compound questions across two real runs — so resampling is the
    appropriate response to a rejection, and the plain call failure the same run
    also hit (`claude exited 1`, CLI healthy a second later) wants the same
    treatment for a different reason.

    The two failure kinds are retried differently. A rejection goes back with the
    offending question attached, because the rule it broke was already in the
    prompt and being ignored. A call failure goes back unchanged, because nothing
    about the prompt caused it.
    """
    base = build_prompt(seed, dialogue)
    prompt = base
    spent = 0.0
    last: LLMError | None = None

    for _ in range(attempts):
        try:
            completion = complete(prompt)
        except LLMError as exc:
            last = exc
            prompt = base
            continue

        spent += completion.cost_usd or 0.0

        try:
            parsed = parse_probe(seed, completion)
        except ProbeRejected as exc:
            last = exc
            prompt = base + CORRECTION.format(question=exc.question, reason=exc.reason)
            continue

        # Every attempt was billed, so every attempt is in the number. A cost that
        # counts only the winning call makes stage 3 look cheaper than it is.
        return replace(parsed, cost_usd=spent)

    raise last if last else LLMError("probe exhausted its attempts without an error")


