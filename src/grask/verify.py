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

    It carries the spend as well as the reason, inherited from `LLMError`: a
    discarded probe leaves no probes row, so this exception is the only thing
    that ever knows what stages 3 and 4 cost on a question nobody will be asked.
    `verify` fills it in on the way past; `adjudicate`, which has no idea what
    anything cost, leaves it None.
    """

    def __init__(
        self,
        reason: str,
        *,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
    ) -> None:
        super().__init__(reason, cost_usd=cost_usd, duration_ms=duration_ms)
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

    The fallback is all-or-nothing, and that is the point. Trusting each `index`
    on its own is only safe while they are distinct: a response that labels
    every element `"index": 0` is individually in range and collectively
    meaningless, and reading it literally moves the true verdict onto option 0
    — which discards a perfectly good probe for disagreeing with a key it never
    actually disagreed with. So the indices are accepted only as a set: a
    permutation of the options, or position throughout.
    """
    parsed = extract_json_array(text, salvage_keys=VERDICT_KEYS)

    if len(parsed) != expected:
        raise LLMError(f"judged {len(parsed)} of {expected} options")

    claimed = [item.get("index") for item in parsed]
    whole = sorted(i for i in claimed if isinstance(i, int) and not isinstance(i, bool))
    permutation = whole == list(range(expected))

    verdicts = []
    for position, item in enumerate(parsed):
        raw = claimed[position]
        index = raw if permutation and isinstance(raw, int) else position

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

    A surviving probe comes back with stage 4 folded into *both* `cost_usd` and
    `duration_ms`. Two columns on one row that covered different sets of stages
    would be two numbers nobody could put beside each other.
    """
    prompt = build_prompt(probe)
    spent = probe.cost_usd or 0.0
    elapsed = probe.duration_ms or 0
    last: LLMError | None = None

    for _ in range(attempts):
        try:
            completion = complete(prompt)
        except LLMError as exc:
            last = exc
            continue

        spent += completion.cost_usd or 0.0
        elapsed += completion.duration_ms or 0

        try:
            verdicts = parse_verdicts(completion.text, len(probe.options))
        except LLMError as exc:
            last = exc
            continue

        try:
            adjudicate(probe, verdicts)
        except ProbeUnverified as exc:
            # The judgment discards the probe; the spend behind it happened
            # anyway. This is the only scope holding both, and past here there
            # is no probes row left to write the number on.
            exc.cost_usd = spent
            exc.duration_ms = elapsed
            raise

        return replace(probe, cost_usd=spent, duration_ms=elapsed)

    # Exhausting the budget keeps the probe — but stage 3's spend is inside
    # `spent`, and a caller that keeps the probe still has to record what
    # getting here cost.
    failed = last if last else LLMError("verification exhausted its attempts without an error")
    failed.cost_usd = spent
    failed.duration_ms = elapsed
    raise failed
