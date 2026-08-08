"""Stage 2: the seed — what was accepted, and the claim about what wasn't understood.

Runs only when triage says yes, on the moment selection already picked. Its
output is stored, which is the point: when the stage-3 prompt improves, every
past seed can be re-run into a better probe without needing transcripts back.
Probe quality is the top engineering risk in this design, so the ability to
re-ask the whole corpus after a prompt change is worth the storage.

The hypothesis is the seed's most important field. Everything downstream hangs
off it — stage 3 derives the rubric from it, and a "not worth asking" vote is
attributed to it. That is what makes a no-vote diagnosable rather than an
unactionable complaint, so a seed without a falsifiable one is rejected rather
than stored.
"""

from __future__ import annotations

from dataclasses import dataclass

from grask.dialogue import Dialogue, Edit, Reply
from grask.llm import Completion, LLMError, complete, extract_json_object
from grask.transcript import Turn, normalize
from grask.triage import Moment

SEED_KEYS = ("decline", "topic", "quotes", "refs", "decision", "hypothesis")

MAX_EVENT_CHARS = 2000
MAX_PROMPT_CHARS = 60_000

# A hypothesis has to assert something that could turn out false. A bare noun
# phrase cannot: `concern` failed as a stage-1 signal for exactly this reason,
# being a subject rather than a claim. Crude proxy — a claim needs a verb, and
# a few words to put it in — but it catches the failure that actually occurs,
# which is the model restating the topic.
MIN_HYPOTHESIS_WORDS = 8


class SeedDeclined(LLMError):
    """Stage 2 read the session and would not assert a misconception.

    A judgment, not a malfunction, and the distinction is the reason this type
    exists. Triage decides a session is worth *looking at*; it never decides
    that a misconception is there, and stage 2 — which is the first stage to see
    the agent's side of the conversation and the diff — is the first thing that
    can tell. Before this it had two exits, invent a hypothesis or trip a
    structural gate, and the second is recorded as a broken pipeline.

    `capture` records it as its own verdict for that reason: folded into
    `error`, a working decline is indistinguishable from a broken model call in
    the one number that says whether the prompt works.
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
class Seed:
    """The stored record of one moment worth asking about."""

    session_id: str
    turn: int
    signal: str
    topic: str
    quotes: tuple[str, ...]
    refs: tuple[str, ...]
    decision: str
    hypothesis: str
    cost_usd: float | None = None
    duration_ms: int | None = None


PROMPT = """\
You are stage 2 of `grask`. Triage has already decided this session is worth one
question and which moment earns it. You are not re-deciding that, and you are not
writing the question.

Your job is to name the most plausible mechanism misconception that the evidence
in this session actually supports, as a falsifiable claim.

Note what that is not. You cannot see what this developer understands — nobody
can, and nothing in this transcript reports it. What you can see is what they
typed, what the agent said, and what shipped. Name the belief those give you
reason to think is mistaken; do not assert a mental state the evidence cannot
carry.

## The moment triage selected

Topic: {topic}
Signal: {signal}
What the developer said: {quote}

## The session

{rendered}

## What to produce

- `topic` — the concept at stake, in the developer's own frame.
- `quotes` — 1 to 3 things the DEVELOPER typed, copied character for character
  from the session above. Not paraphrased, not tidied, not the agent's words.
  A quote that is not in the session is discarded and may cost the whole seed.
- `refs` — `file:line` for what shipped, where visible. Empty list if not.
- `decision` — the specific thing that landed in their codebase.
- `hypothesis` — THE IMPORTANT ONE. The misconception the evidence supports,
  specific enough to be WRONG.
  Aim it at the technical mechanism at the moment's core — API semantics, tool
  behavior, data formats, configuration effects, algorithms — rather than at a
  behavioural or process reading of the same moment. A claim about a mechanism
  can be settled by an answer; a claim about process cannot.
  The mechanism must outlive this repository. How a query language parses an
  expression, what an API returns at a boundary, what a format cannot encode —
  those are gaps worth closing anywhere. What one step of a local file does is
  recall: answerable only by having been in the session, and worth nothing once
  the developer closes that file. Local code is where the gap SHOWED UP, never
  what the gap is ABOUT.

A hypothesis must be a sentence that asserts something. "Idempotency keys" is a
topic, not a hypothesis. "The developer accepted that a key prevents double
charges without knowing the key must be stable across retries to do so" is a
hypothesis: it names the belief, the gap, and would be refuted by an answer that
mentions stability.

Do not hedge it into safety. "May not fully understand the implications" is
unfalsifiable and therefore useless — it can never be shown wrong, so no answer
can ever settle it.

## What is and is not evidence

Do not infer a gap merely because:

- the mechanism was introduced by the agent rather than by the developer
- the developer asked a question
- the developer accepted a suggestion
- the code uses a pattern that looks unfamiliar

None of those is evidence about a belief. They are evidence that something
happened.

Prefer a hypothesis supported by one of:

- the developer putting the mechanism in their own words and getting it wrong or
  leaving out the part that makes it work
- a question the developer asked that the session never actually answered
- a disagreement that reveals a mistaken causal model
- a decision followed by language showing they were unsure of it

## If the evidence does not support one

Triage decided this session was worth looking at. It did not decide that a
misconception is here, and you are allowed to disagree with it — you can see the
agent's side of the conversation and what shipped, and triage could not.

If nothing here supports a specific mechanism misconception — the moment was
real but the developer plainly had it right, or the mechanism is too thin to be
wrong about — reply with exactly this and nothing else:

{{"decline": "one sentence naming what the evidence was missing"}}

A declined seed is silence, and silence is a correct outcome. It costs far less
than a question invented to fill the slot: that question gets asked, wastes the
developer's twenty seconds, and teaches them the tool is guessing.

## Respond

Otherwise, one JSON object, nothing else:

{{"topic": "...", "quotes": ["..."], "refs": ["..."], "decision": "...", \
"hypothesis": "..."}}
"""


def render_dialogue(dialogue: Dialogue) -> str:
    """The session as the model sees it: who said what, and what changed, in order."""
    lines: list[str] = []
    for event in dialogue.events:
        if isinstance(event, Turn):
            lines.append(f"[{event.index}] DEVELOPER: {event.text[:MAX_EVENT_CHARS]}")
        elif isinstance(event, Reply):
            lines.append(f"[{event.index}] AGENT: {event.text[:MAX_EVENT_CHARS]}")
        elif isinstance(event, Edit):
            body = f"[{event.index}] EDIT {event.file_path}"
            if event.before:
                body += f"\n--- before\n{event.before}"
            body += f"\n+++ after\n{event.after}"
            lines.append(body)

    rendered = "\n\n".join(lines)
    if len(rendered) > MAX_PROMPT_CHARS:
        # Keep the tail: the moment triage selected is usually late in the
        # session, and the decision it produced is later still.
        rendered = "… [earlier turns omitted]\n\n" + rendered[-MAX_PROMPT_CHARS:]
    return rendered


def build_prompt(dialogue: Dialogue, moment: Moment) -> str:
    return PROMPT.format(
        topic=moment.topic,
        signal=moment.signal,
        quote=moment.quote,
        rendered=render_dialogue(dialogue),
    )


def verified_quotes(dialogue: Dialogue, claimed: object) -> tuple[str, ...]:
    """Keep only quotes that actually appear in something the developer typed.

    Instruction is not a control. The model is told to copy verbatim and mostly
    does; this is what makes it true rather than likely.
    """
    if not isinstance(claimed, list):
        return ()
    spoken = [normalize(turn.text) for turn in dialogue.turns]
    kept = []
    for quote in claimed:
        if not isinstance(quote, str) or not quote.strip():
            continue
        needle = normalize(quote)
        if any(needle in haystack for haystack in spoken):
            kept.append(quote.strip())
    return tuple(kept)


def parse_seed(dialogue: Dialogue, moment: Moment, completion: Completion) -> Seed:
    """Read a seed, refusing the two shapes that would poison everything downstream.

    Rejects rather than repairs. A seed is stored and re-run for the life of the
    corpus, so a bad one is not a bad question once — it is a bad question every
    time the prompt improves.
    """
    parsed = extract_json_object(completion.text, salvage_keys=SEED_KEYS)

    # Checked first, and only when it carries a reason. A blank `decline` is a
    # mangled response rather than a judgment, and reading it as one would let a
    # degraded model call empty the queue while looking like restraint.
    declined = parsed.get("decline")
    if isinstance(declined, str) and declined.strip():
        raise SeedDeclined(declined.strip())

    hypothesis = parsed.get("hypothesis")
    if not isinstance(hypothesis, str) or len(hypothesis.split()) < MIN_HYPOTHESIS_WORDS:
        raise LLMError(f"hypothesis missing or not a claim: {str(hypothesis)[:200]!r}")

    quotes = verified_quotes(dialogue, parsed.get("quotes"))
    if not quotes:
        raise LLMError("no claimed quote appears in the developer's own turns")

    refs = parsed.get("refs")
    topic = parsed.get("topic")
    decision = parsed.get("decision")

    return Seed(
        session_id=dialogue.session_id,
        turn=moment.turn,
        signal=moment.signal,
        topic=topic if isinstance(topic, str) and topic.strip() else moment.topic,
        quotes=quotes,
        refs=tuple(r for r in refs if isinstance(r, str)) if isinstance(refs, list) else (),
        decision=decision.strip() if isinstance(decision, str) else "",
        hypothesis=hypothesis.strip(),
        cost_usd=completion.cost_usd,
        duration_ms=completion.duration_ms,
    )


def seed(dialogue: Dialogue, moment: Moment) -> Seed:
    """Turn a triaged moment into a stored, re-runnable hypothesis.

    Raises `LLMError` rather than inventing a seed. Unlike triage, which must
    stay silent inside a hook, a failure here is a bug report against the prompt
    and the caller needs to see it.

    A rejected seed was still a paid-for call, so the failure carries what it
    cost. Only a parse failure can: if `complete` itself raised, the call did
    not get far enough to have a price.
    """
    completion = complete(build_prompt(dialogue, moment))
    try:
        return parse_seed(dialogue, moment, completion)
    except LLMError as exc:
        exc.cost_usd = completion.cost_usd
        exc.duration_ms = completion.duration_ms
        raise
