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

import re
from dataclasses import dataclass

from grill.dialogue import Dialogue, Edit, Reply
from grill.llm import Completion, LLMError, complete, extract_json_object
from grill.transcript import Turn
from grill.triage import Moment

SEED_KEYS = ("topic", "quotes", "refs", "decision", "hypothesis")

MAX_EVENT_CHARS = 2000
MAX_PROMPT_CHARS = 60_000

# A hypothesis has to assert something that could turn out false. A bare noun
# phrase cannot: `concern` failed as a stage-1 signal for exactly this reason,
# being a subject rather than a claim. Crude proxy — a claim needs a verb, and
# a few words to put it in — but it catches the failure that actually occurs,
# which is the model restating the topic.
MIN_HYPOTHESIS_WORDS = 8


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
You are stage 2 of `grill`. Triage has already decided this session is worth one
question and which moment earns it. You are not re-deciding that, and you are not
writing the question.

Your job is to state, as a falsifiable claim, what this developer may have
accepted without fully understanding.

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
- `hypothesis` — THE IMPORTANT ONE. A claim about what they do not understand,
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

## Respond

One JSON object, nothing else:

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


def _normalize(text: str) -> str:
    """Collapse whitespace for comparison only.

    Verification must not be so literal that a re-wrapped genuine quote fails.
    Failing true quotes would push us toward trusting the model instead, which
    is the wrong direction to be pushed.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def verified_quotes(dialogue: Dialogue, claimed: object) -> tuple[str, ...]:
    """Keep only quotes that actually appear in something the developer typed.

    Instruction is not a control. The model is told to copy verbatim and mostly
    does; this is what makes it true rather than likely.
    """
    if not isinstance(claimed, list):
        return ()
    spoken = [_normalize(turn.text) for turn in dialogue.turns]
    kept = []
    for quote in claimed:
        if not isinstance(quote, str) or not quote.strip():
            continue
        needle = _normalize(quote)
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
    """
    return parse_seed(dialogue, moment, complete(build_prompt(dialogue, moment)))
