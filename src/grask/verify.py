"""Stage 4: an independent judgment on whether the answer key is actually right.

Stage 3 writes the question, all four options, the key, and the explanation in
one call, in sequence, and never re-reads an early option against a later one.
The prompt already forbids the failures that produces — overlapping options, a
key asserting something the artifact does not show — so more instruction is the
approach that has already been tried. This is the check instead.

It takes a `Probe` and nothing else. No `Seed`, no `Dialogue`, no repository: a
judge that has seen the reasoning behind the answer is the model agreeing with
itself, and keeping the transcript unavailable at the type level is what stops
that eroding into a rubber stamp. The signature is the control.

What it does NOT do is arbitrate. When the judge names a single true option that
is not the stored key, the probe is discarded rather than repointed. Measured
over the 47 stored probes, that judgment landed 3 times: once on a genuinely
false key (a PEP 503 question whose key claimed `fault-line` normalises onto
`faultline` — it does not), and twice on questions about local files the judge
could only reason about from the stem's own premises, where the option it
preferred was false. Repointing would have fixed the first and installed a
falsehood in the other two. Discarding is right at that ratio, and it is right
for a second reason: an option the judge cannot separate from the key is a
question not worth a developer's twenty seconds either way.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from grask.llm import LLMError, complete, extract_json_array
from grask.probe import Probe

VERDICT_KEYS = ("index", "true", "reason")

# Retries here are for a mangled response, never for a judgment. Three matches
# the rest of the pipeline; the observed failure was 2 of 47 and both were the
# same shape (the judge answering in prose instead of JSON).
MAX_ATTEMPTS = 3

LETTERS = "abcdefgh"


class ProbeUnverified(LLMError):
    """The judge read the options and the key does not survive.

    A subclass of `LLMError` so that a caller which only knows "stage 4 failed"
    still handles it, but a distinct type because the two failures have opposite
    consequences: this discards the probe, a plain `LLMError` keeps it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Verdict:
    """One option, judged on its own."""

    index: int
    true: bool
    reason: str


# The wording carries three loads, each one paid for by something the backtest
# did. "Not to pick the best answer" and "do not assume exactly one is true",
# because a judge given four options will otherwise rank them and hand back the
# winner — which reproduces stage 3's blind spot at twice the price. The
# required reason, because an unreasoned boolean is a coin flip that reads like
# a judgment. And the no-tools paragraph, because two backtested probes came
# back as an attempted `bash` call instead of JSON: the judge went looking for
# the repository, which it must never be answering from anyway.
PROMPT = """\
You are verifying a multiple-choice question about a software mechanism. Your job
is NOT to pick the best answer. Your job is to judge each option on its own.

For EACH option, decide whether it is a TRUE answer to the question — that is,
whether the mechanism it asserts is factually correct AND actually answers what
the question asks. An option that states something true about the world but does
not answer the question is FALSE. An option that answers the question with a
mechanism that does not work that way is FALSE.

Do not assume exactly one option is true. Two options can both be true — that is
one of the defects you are here to find. Zero can be true. Judge each one as if
it were the only statement in front of you.

You must give a reason for every verdict, naming the specific fact that makes the
option true or false. "It is the best answer" is not a reason. "It is a
distractor" is not a reason.

You have no tools and no access to any repository, file, or transcript. Judge
from the text below and from general knowledge of how the named technology works.
Where an option depends on a local detail you cannot check, take the question's
own stated premises as given and judge the mechanism it asserts on top of them.

## The question

{question}

## The options

{options}

## Respond

One JSON array, nothing else, one object per option in the order given:

[{{"index": 0, "true": false, "reason": "..."}}]
"""


def build_prompt(probe: Probe) -> str:
    options = "\n".join(f"({LETTERS[i]}) {o}" for i, o in enumerate(probe.options))
    return PROMPT.format(question=probe.question, options=options)


def parse_verdicts(text: str, expected: int) -> tuple[Verdict, ...]:
    """One verdict per option, or an error worth retrying.

    Position is the fallback for `index`: the model is asked for the array in
    order, and an off-by-one in a field it filled in by hand should not cost a
    probe when the ordering already carries the same information.
    """
    parsed = extract_json_array(text, salvage_keys=VERDICT_KEYS)

    if len(parsed) != expected:
        raise LLMError(f"judged {len(parsed)} of {expected} options")

    verdicts = []
    for position, item in enumerate(parsed):
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected:
            index = position

        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise LLMError(f"option {index} was judged without a reason")

        verdicts.append(Verdict(index=index, true=bool(item.get("true")), reason=reason.strip()))

    return tuple(verdicts)


def adjudicate(probe: Probe, verdicts: tuple[Verdict, ...]) -> None:
    """Raise unless exactly one option is true and it is the stored key."""
    true = [v for v in verdicts if v.true]
    reasons = "; ".join(f"({LETTERS[v.index]}) {v.reason}" for v in true)

    if not true:
        raise ProbeUnverified(f"no option was judged true: {probe.question!r}")

    if len(true) > 1:
        raise ProbeUnverified(
            f"{len(true)} options were judged true, {[v.index for v in true]}: {reasons}"
        )

    if true[0].index != probe.correct_idx:
        raise ProbeUnverified(
            f"the only true option is option {true[0].index}, "
            f"not the stored key {probe.correct_idx}: {reasons}"
        )


def verify(probe: Probe, *, complete=complete, attempts: int = MAX_ATTEMPTS) -> Probe:
    """Return the probe if its key survives an independent reading, else raise.

    A mangled response is resampled; a judgment never is. Retrying a judgment
    would be rolling the dice until the probe passes, which is the same rubber
    stamp the transcript-free signature exists to prevent — the first honest
    answer is the answer.
    """
    prompt = build_prompt(probe)
    spent = probe.cost_usd or 0.0
    last: LLMError | None = None

    for _ in range(attempts):
        try:
            completion = complete(prompt)
        except LLMError as exc:
            last = exc
            continue

        spent += completion.cost_usd or 0.0

        try:
            verdicts = parse_verdicts(completion.text, len(probe.options))
        except LLMError as exc:
            last = exc
            continue

        adjudicate(probe, verdicts)
        return replace(probe, cost_usd=spent)

    raise last if last else LLMError("verification exhausted its attempts without an error")
