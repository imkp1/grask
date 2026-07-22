"""Stage 1: is there anything here worth asking about?

Runs on every session that survives stage 0. Writes nothing, stores nothing,
and for most sessions answers "no" — see "Silence is a valid outcome" in the
design doc. This is the first stage that costs money.

Stage 1 sees the developer's turns and the *paths* of files touched, never file
contents. Code is the expensive input (~36KB median) and it is what stages 2 and
3 need to ground a probe. Deciding *whether* a session has an engaged-with
concept is answerable from what the developer typed, at ~1.3KB per session.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from grill.llm import Completion, LLMError, complete, extract_json_array
from grill.select import select
from grill.transcript import Session, Turn

# One pasted stack trace should not crowd out fifteen short turns. Generous
# enough that ordinary prose is never cut.
MAX_TURN_CHARS = 3000
MAX_PROMPT_CHARS = 24_000
MAX_FILES_LISTED = 40

# The four qualifying signals from the design's "What counts as a topic", split
# by whether a quote can actually prove them.
#
# For the first two the developer's own words *are* the evidence: a why-question
# or a correction is visible in the quote itself. For the second two the quote
# can only ever be circumstantial — a pattern landing in the codebase is shown by
# the code, not by anything the developer typed — so a keep on those grounds is
# recorded as weak and left for stage 2 to ground in the diff.
QUOTE_PROVABLE = frozenset({"asked_why", "pushed_back"})
CODE_GROUNDED = frozenset({"new_pattern", "explained_at_length"})
VALID_SIGNALS = QUOTE_PROVABLE | CODE_GROUNDED

# Per-moment keys, in response order. Declared in the order they appear in a
# response, which is what lets a flat salvage parse recover values containing
# unescaped quotation marks.
MOMENT_KEYS = ("turn", "signal", "topic", "quote", "shows")

PROMPT = """\
You are the triage stage of `grill`, a tool that asks a developer ONE question at
the end of a coding session about something they may not actually understand.

Your job is to list EVERY moment in this session that would qualify as worth
asking about. You do not write the question, and you do not choose between
moments — something else ranks them. List them all, in the order they occur.

Most sessions contain nothing that qualifies. An empty list is the common answer
and the correct one, not a failure to find something. A weak moment is worse than
no moment: a generic question about something the developer has done a thousand
times is how this tool gets disabled in week two. Do not pad the list.

## What qualifies

Something the developer ENGAGED with. Each moment is exactly one of these four
signals, named as `signal`:

- `asked_why` — they asked why about something. Their curiosity, not the agent's
  output. The strongest signal.
- `pushed_back` — they corrected, overrode, or disagreed with the agent. Judgment
  showing.
- `new_pattern` — a pattern, library, or technique was newly introduced into
  their code.
- `explained_at_length` — the agent explained something at length and the
  developer took it on board.

For the last two the test is: did something land in this developer's codebase
whose *rationale* they may have accepted on faith?

## What to prefer

Prefer moments whose core is a technical mechanism: API semantics, tool
behavior, data formats, configuration effects, algorithms. Down-rank
behavioural or process moments — why a message was phrased a certain way,
workflow or etiquette choices, why the assistant took the approach it took.
A mechanism has a right answer a question can test; a process choice mostly
does not.

## What does not qualify

Activity is not learning. None of the following qualify on their own:

- files edited, commands run, tests made to pass
- config changes, dependency bumps, renames, formatting, lint fixes
- a bug the developer diagnosed themselves — they already understand it
- the developer directing work they plainly already know how to do
- writing prose, documentation, specs, or commit messages
- sessions where the developer only said things like "yes", "continue", "go on",
  "fix it", "thanks", or pasted an error and accepted the fix

## Evidence rule

Every moment MUST quote the developer verbatim — the exact words, copied
character for character — and MUST name the bracketed turn number `[n]` that
quote appears in. The quote must come from that turn and no other. Only the
developer's own words appear below; there is nothing else to quote. If you
cannot produce such a quote, the moment does not go in the list.

No quote, no moment.

The quote must be evidence for the signal you named. `shows` describes what
*that quote* demonstrates — not what the session was about. If you find yourself
writing `shows` about something the quote does not contain, you have picked the
wrong quote or the wrong signal.

For `asked_why` the quote must be the developer asking. For `pushed_back` it must
be the developer correcting or overriding.

## Output

Reply with a single JSON array and nothing else. Escape every quotation mark that
appears inside a string value. For no qualifying moments, reply `[]`.

[
  {{"turn": <the number in brackets, as an integer>,
    "signal": "asked_why" | "pushed_back" | "new_pattern" | "explained_at_length",
    "topic": "short noun phrase naming the concept",
    "quote": "verbatim developer words from that turn",
    "shows": "one sentence on what this quote demonstrates"}}
]

## Session

{session}
"""


@dataclass
class Moment:
    """One thing in a session that could earn a question.

    `turn` is the moment's identity. Anchoring to a turn rather than to the topic
    wording is what makes two runs comparable: the same moment comes back
    described differently every time, but from the same turn.
    """

    turn: int
    signal: str
    topic: str
    quote: str
    shows: str
    # Kept, but on a signal the quote cannot prove. Recorded rather than dropped:
    # stage 2 has to ground these in the diff before they earn a question.
    weak_evidence: bool = False


@dataclass
class TriageVerdict:
    """Stage 1's answer for one session."""

    session_id: str
    verdict: str  # "ask" | "silent"
    signal: str | None = None
    topic: str | None = None
    quote: str | None = None
    reason: str = ""
    cost_usd: float | None = None
    duration_ms: int | None = None
    error: str | None = None
    # Set when the model said "ask" but the evidence rule demoted it. A high rate
    # here is a bug report against the prompt, not against the developer.
    demoted_from_ask: bool = False
    # Kept, but on a signal the quote cannot prove. Recorded rather than dropped:
    # these are the keeps whose justification outruns their evidence, and stage 2
    # has to ground them in the diff before they earn a question.
    weak_evidence: bool = False
    # Every qualifying moment, not just the selected one. Stage 2 gets the
    # runner-up for free, and dedup gets something to work with when the top
    # moment has already been asked about.
    moments: list[Moment] = field(default_factory=list)
    candidates: int = 0

    @property
    def kept(self) -> bool:
        return self.verdict == "ask"


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def render_session(session: Session) -> str:
    """The session as stage 1 sees it: what was typed, and what was touched."""
    lines: list[str] = []
    if session.git_branch:
        lines.append(f"branch: {session.git_branch}")
    lines.append(f"developer turns: {len(session.turns)}")

    if session.files_touched:
        shown = sorted(session.files_touched)[:MAX_FILES_LISTED]
        extra = len(session.files_touched) - len(shown)
        lines.append(
            f"files edited ({len(session.files_touched)}): "
            + ", ".join(shown)
            + (f", ... and {extra} more" if extra else "")
        )
    else:
        lines.append("files edited: none")

    lines.append("")
    lines.append("--- what the developer typed ---")
    for turn in session.turns:
        text = turn.text
        if len(text) > MAX_TURN_CHARS:
            dropped = len(turn.text) - MAX_TURN_CHARS
            text = text[:MAX_TURN_CHARS] + f"\n[... {dropped} chars truncated]"
        lines.append(f"[{turn.index}] {text}")

    rendered = "\n".join(lines)
    if len(rendered) > MAX_PROMPT_CHARS:
        rendered = rendered[:MAX_PROMPT_CHARS] + "\n[... session truncated]"
    return rendered


def build_prompt(session: Session) -> str:
    return PROMPT.format(session=render_session(session))


def parse_moments(session: Session, completion: Completion) -> tuple[list[Moment], list[str]]:
    """Validate every moment the model reported, enforcing the evidence rule.

    Prompting a model to require a quote is not a control; checking the quote is.
    The check is stricter than it was when one verdict covered the session: the
    quote must appear in the *turn the model named*, not merely somewhere in the
    session. That is what makes the turn index trustworthy as a moment's
    identity, which selection and dedup both depend on.

    Rejections are returned rather than raised. One bad moment in a list of six
    is a bad moment, not a failed session.
    """
    by_index = {turn.index: turn for turn in session.turns}
    found: list[Moment] = []
    rejected: list[str] = []

    for raw in extract_json_array(completion.text, salvage_keys=MOMENT_KEYS):
        turn_index = raw.get("turn")
        if isinstance(turn_index, str) and turn_index.strip().lstrip("[").rstrip("]").isdigit():
            turn_index = int(turn_index.strip().lstrip("[").rstrip("]"))
        quote = raw.get("quote")
        quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
        signal = raw.get("signal")
        signal = signal.strip().lower() if isinstance(signal, str) and signal.strip() else None
        topic = raw.get("topic")
        topic = topic.strip() if isinstance(topic, str) and topic.strip() else None
        shows = str(raw.get("shows") or "").strip()

        if not isinstance(turn_index, int) or turn_index not in by_index:
            # Build the message from what the model actually sent, not from a
            # turn number that was never valid. "turn None: no turn None in
            # this session" was unreadable when `turn` was missing or not a
            # number; only a validated int earns the "turn N" phrasing below.
            if isinstance(turn_index, int):
                rejected.append(f"turn {turn_index}: no turn {turn_index} in this session")
            else:
                rejected.append(
                    f"missing or non-integer turn ({turn_index!r}): "
                    "no matching turn in this session"
                )
            continue

        label = f"turn {turn_index}"
        if signal not in VALID_SIGNALS:
            rejected.append(f"{label}: unrecognized signal {signal!r}")
            continue
        if quote is None or _normalize(quote) not in _normalize(by_index[turn_index].text):
            rejected.append(f"{label}: quote not found in that turn")
            continue
        if signal == "asked_why" and not Turn(text=quote, timestamp=None, index=0).is_question:
            rejected.append(f"{label}: signal is asked_why but the quote asks nothing")
            continue

        found.append(
            Moment(
                turn=turn_index,
                signal=signal,
                topic=topic or "",
                quote=quote,
                shows=shows,
                weak_evidence=signal in CODE_GROUNDED,
            )
        )

    return found, rejected


def triage(session: Session) -> TriageVerdict:
    """Decide whether this session is worth one question, and which moment earns it.

    Two steps with a seam between them: the model enumerates and evidences every
    qualifying moment, then `select` picks one with no model involved. Selection
    used to happen inside the same call, which made the topic vary run to run on
    an unchanged session.

    Never raises. A stage that can crash the hook is a stage that speaks on
    failure, and the design says the hook must fail silently.
    """
    if not session.turns:
        return TriageVerdict(
            session_id=session.session_id,
            verdict="silent",
            reason="no developer turns (stage 0 floor)",
        )

    try:
        completion = complete(build_prompt(session))
    except LLMError as exc:
        # The model call itself never happened or never came back — nothing to
        # diagnose in a completion because there isn't one.
        return TriageVerdict(
            session_id=session.session_id,
            verdict="silent",
            reason="triage failed: model call failed",
            error=str(exc),
        )

    try:
        moments, rejected = parse_moments(session, completion)
    except LLMError as exc:
        # The model answered, but nothing in the response was salvageable as a
        # JSON array even after per-element recovery. Distinct from the
        # transport failure above: this one is a bug report against the
        # prompt or the parser, not against the CLI or the network.
        return TriageVerdict(
            session_id=session.session_id,
            verdict="silent",
            reason="triage failed: could not parse response",
            error=str(exc),
            cost_usd=completion.cost_usd,
            duration_ms=completion.duration_ms,
        )

    verdict = TriageVerdict(
        session_id=session.session_id,
        verdict="silent",
        moments=moments,
        candidates=len(moments),
        cost_usd=completion.cost_usd,
        duration_ms=completion.duration_ms,
    )

    chosen = select(moments)
    if chosen is None:
        # Nothing survived. Distinguish "the model found nothing", which is the
        # system working, from "everything it found failed the evidence rule",
        # which is a bug report against the prompt.
        verdict.demoted_from_ask = bool(rejected)
        verdict.reason = (
            f"demoted: {'; '.join(rejected)}" if rejected else "no qualifying moments"
        )
        return verdict

    verdict.verdict = "ask"
    verdict.signal = chosen.signal
    verdict.topic = chosen.topic
    verdict.quote = chosen.quote
    verdict.reason = chosen.shows
    verdict.weak_evidence = chosen.weak_evidence
    return verdict
