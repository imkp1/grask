"""One probe, asked and graded mechanically.

Pure logic. The console is injected, exactly as `capture_session` injects its
stages, so the whole flow is drivable by a scripted console. No argparse, no
terminal control codes, no TTY — those belong to `cli.py`, and keeping them out
is what leaves the delivery question open. `ask` does not care whether a human
typed `grask` or a hook called it.

There is no model call anywhere in this path. Picking an option IS the answer:
the verdict is `pick == correct_idx`, the explanation was written at generation
time, and the only `error` outcome left is a stored row too malformed to serve.
That is a property of the design, not of careful coding — a judge cannot be
slow, expensive, or cowardly if there is no judge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from grask.probe import LETTERS, Rubric

# Written to a TEXT column and grouped on by hand later, so the strings are the
# schema. `premise_rejected` is its own outcome rather than a flavour of skip:
# it is the zealot rate, and a rate you cannot query is a rate nobody checks.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
PREMISE_REJECTED = "premise_rejected"
ERROR = "error"

WRONG = "/wrong"

# `LETTERS` is imported rather than defined here: see `probe.LETTERS`, which
# owns it because options are what `probe` produces.


class Console(Protocol):
    """Everything the interrogation needs from the outside world."""

    def show(self, text: str) -> None: ...

    def prompt(self, text: str) -> str: ...


@dataclass(frozen=True)
class PendingProbe:
    """A stored probe, ready to be asked.

    Lives here rather than in `storage.py` so that the import runs
    storage -> ask, matching how storage already imports `Probe` and `Seed` from
    the modules that produce them, and avoiding the cycle the other direction
    would create.

    `correct_idx` is `int | None` because the row is trusted nowhere: a mangled
    row surfaces as `None`, an empty tuple, or an out-of-range index, and `ask`
    records `error` instead of guessing.
    """

    probe_id: int
    question: str
    options: tuple[str, ...]
    correct_idx: int | None
    explanation: str
    rubric: Rubric
    created_at: str


@dataclass(frozen=True)
class AnswerTurn:
    """One developer turn: the question, and the option text they chose."""

    turn: int
    question: str
    answer: str


@dataclass(frozen=True)
class Interrogation:
    """Everything that happened, ready to be stored. No database in sight."""

    probe_id: int
    outcome: str
    objection: str | None
    turns: tuple[AnswerTurn, ...]
    cost_usd: float | None


OBJECTION_PROMPT = "what's wrong with it? (enter to skip)"
MALFORMED = "this probe's stored options are unusable; recording an error."


def context_line(pending: PendingProbe) -> str:
    """One line of orientation above the question.

    Without it the developer reads a question about work they cannot place, which
    is the version of this tool that feels like a quiz. `created_at` is the
    probe's, which is the session's within a few seconds of it.
    """
    when = pending.created_at[:10]
    return f"from {when} · {pending.rubric.topic}"


def render_options(options: tuple[str, ...]) -> str:
    return "\n".join(f"  {LETTERS[i]}) {option}" for i, option in enumerate(options))


def pick_prompt(count: int) -> str:
    return f"pick   [a-{LETTERS[count - 1]}]   ·   enter = skip   ·   /wrong"


def pick_hint(count: int) -> str:
    return f"type a single letter, a-{LETTERS[count - 1]}."


def unservable(pending: PendingProbe) -> bool:
    """A row the ask cannot honestly grade.

    Storage already filters `options IS NULL`, so what arrives here failed to
    parse or carries an index that names no option. Grading either would invent
    a verdict, which is worse than admitting the row is broken.
    """
    return (
        len(pending.options) < 2
        or len(pending.options) > len(LETTERS)
        or pending.correct_idx is None
        or not 0 <= pending.correct_idx < len(pending.options)
        or not pending.explanation.strip()
    )


def resolution(
    pending: PendingProbe,
    outcome: str,
    *,
    objection: str | None = None,
    turns: tuple[AnswerTurn, ...] = (),
) -> Interrogation:
    """An Interrogation for `pending`, however it ended.

    Shared by the interactive loop and the non-interactive record path so both
    write identically shaped rows. `cost_usd` is always 0.0: there is no model
    call anywhere in an ask, whichever surface drove it.
    """
    return Interrogation(
        probe_id=pending.probe_id,
        outcome=outcome,
        objection=objection,
        turns=turns,
        cost_usd=0.0,
    )


def grade(pending: PendingProbe, pick: str) -> Interrogation:
    """(pending, pick letter) -> a mechanically graded Interrogation.

    The one place a pick becomes a verdict; `ask` and `grask record` both land
    here. Raises ValueError rather than guessing on bad input — the caller
    decides how to surface it. Never call this on a row `unservable` flags.
    """
    valid = LETTERS[: len(pending.options)]
    letter = pick.strip().lower()
    if len(letter) != 1 or letter not in valid:
        raise ValueError(f"pick must be a single letter, a-{valid[-1]}")
    picked = valid.index(letter)
    return resolution(
        pending,
        PASSED if picked == pending.correct_idx else FAILED,
        turns=(AnswerTurn(turn=0, question=pending.question, answer=pending.options[picked]),),
    )


PLAIN = "plain"
MARKDOWN = "markdown"

# What each outcome calls itself. `skipped` gets no verdict word: it is not a
# wrong answer, and design.md's rule that one probe cannot identify
# understanding cuts both ways — nothing here may read as a score.
VERDICTS = {
    PASSED: "✓ Correct",
    FAILED: "✗ Incorrect",
    SKIPPED: "— Skipped",
    PREMISE_REJECTED: "— Premise rejected",
    ERROR: "— Unusable probe",
}


def _picked_idx(pending: PendingProbe, graded: Interrogation) -> int | None:
    """Which option was chosen, recovered from the stored turn.

    The turn holds the option *text*, because that is what a stored row has to
    survive a reshuffle. Two options with identical text would resolve to the
    first, which renders the same string either way.
    """
    if not graded.turns:
        return None
    try:
        return pending.options.index(graded.turns[0].answer)
    except ValueError:
        return None


def result_block(pending: PendingProbe, graded: Interrogation, *, style: str = PLAIN) -> str:
    """The whole of what a developer sees after a pick, for every surface.

    The one renderer. A surface that composed its own would drift from this one
    the first time either was edited — which is exactly how the `/grask` skill
    ended up printing a bare `✗` on a line by itself while the terminal printed
    a verdict and an explanation together.

    The two styles differ only where the surrounding surface differs:

    - **topic**: the terminal already printed it above the question, in
      `context_line`. The skill deliberately withholds it until the answer is
      settled, so only `markdown` carries it here.
    - **the pick**: the terminal still has the options on screen, so naming the
      letter is enough. The skill's picker is gone by now, so `markdown` spells
      both options out in full.

    Everything else — the order, the wording, which outcome says what — is
    shared, which is the point.
    """
    md = style == MARKDOWN
    verdict = VERDICTS.get(graded.outcome, VERDICTS[ERROR])
    correct = pending.correct_idx
    picked = _picked_idx(pending, graded)

    # A disputed premise gets no key: the developer's claim is that the question
    # is wrong, and answering it with its own answer key argues past them.
    if graded.outcome in (PREMISE_REJECTED, ERROR) or correct is None:
        return f"**{verdict}**" if md else verdict

    def option(idx: int) -> str:
        return f"{LETTERS[idx]}) {pending.options[idx]}"

    lines = [f"**{verdict}**" if md else verdict]
    if md:
        if graded.outcome == FAILED and picked is not None:
            lines += [
                "",
                f"- **You picked** — {option(picked)}",
                f"- **Correct** — {option(correct)}",
            ]
        elif graded.outcome == SKIPPED:
            lines += ["", f"- **Answer** — {option(correct)}"]
    else:
        if graded.outcome == FAILED and picked is not None:
            lines[0] = (
                f"{verdict} · you picked {LETTERS[picked]}, "
                f"the answer was {LETTERS[correct]}"
            )
            lines += ["", f"  {option(correct)}"]
        elif graded.outcome == SKIPPED:
            lines[0] = f"{verdict} · the answer was {LETTERS[correct]}"
            lines += ["", f"  {option(correct)}"]

    lines += ["", pending.explanation]
    if md:
        lines += ["", f"*This came up from: {pending.rubric.topic}*"]
    return "\n".join(lines)


def ask(pending: PendingProbe, console: Console) -> Interrogation:
    """Run one probe to a verdict: pick, mechanical grade."""

    def done(
        outcome: str,
        *,
        objection: str | None = None,
        turns: tuple[AnswerTurn, ...] = (),
    ) -> Interrogation:
        """Resolve, and show the developer how it ended.

        Every exit from the loop below renders — a skip included. Skipping is
        usually "I don't know", which is precisely when the explanation is worth
        most, and the probe is spent either way: `UNIQUE(probe_id)` means it will
        not be asked again, so withholding the payoff only loses it.
        """
        graded = resolution(pending, outcome, objection=objection, turns=turns)
        console.show(result_block(pending, graded))
        return graded

    def ask_objection() -> str | None:
        """Shared `/wrong` handling: prompt once for an optional reason.

        Optional because requiring an argument to escape is how you get an escape
        hatch nobody uses. The outcome is the signal; the text is a bonus.
        """
        typed = console.prompt(OBJECTION_PROMPT).strip()
        return typed or None

    if unservable(pending):
        # Renders its own line: `result_block` cannot speak for a row whose
        # options or key it just refused to trust.
        console.show(MALFORMED)
        return resolution(pending, ERROR)

    console.show(context_line(pending))
    console.show(pending.question)
    console.show(render_options(pending.options))

    valid = LETTERS[: len(pending.options)]
    while True:
        typed = console.prompt(pick_prompt(len(pending.options))).strip().lower()
        if not typed:
            return done(SKIPPED)
        if typed == WRONG:
            return done(PREMISE_REJECTED, objection=ask_objection())
        if len(typed) == 1 and typed in valid:
            break
        console.show(pick_hint(len(pending.options)))

    graded = grade(pending, typed)
    console.show(result_block(pending, graded))
    return graded
