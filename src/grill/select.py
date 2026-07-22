"""Stage 1 selection: which of a session's qualifying moments earns the question.

Split out of triage because it must be reproducible. Measured over 6 sessions x 3
runs, the LLM finds substantially the same moments every time — 26 of 29 keep
their signal, and topic wording is stable — but a session carries 2-9 qualifying
moments and a single call picks among them arbitrarily. That arbitrary pick was
the whole of the observed topic instability. Ranking is therefore code, not
prompt.

Ordering deviates from the design doc's "What counts as a topic", which ranks
`explained_at_length` and `new_pattern` above `pushed_back`. Those two are
exactly the signals a quote cannot prove (`triage.CODE_GROUNDED`), and preferring
them would make selection favour the weakest evidence available. Signals whose
quote is self-proving come first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

# Lower is better.
SIGNAL_RANK = {
    "asked_why": 0,  # the developer's own curiosity; the quote is the evidence
    "pushed_back": 1,  # judgment showing; also provable from the quote
    "new_pattern": 2,  # circumstantial — stage 2 must ground it in the diff
    "explained_at_length": 3,  # weakest: the agent talked, which is not learning
}

# A signal outside SIGNAL_RANK cannot win. Parsing rejects unknown signals before
# selection sees them, so this only guards against the two drifting apart.
UNRANKED = len(SIGNAL_RANK)


class Rankable(Protocol):
    signal: str
    turn: int


def rank_key(moment: Rankable) -> tuple[int, int]:
    """Sort key for one moment. Lower sorts first.

    Depends only on the moment itself — never on the other candidates. That is
    what makes the winner stable when extraction adds or drops a marginal moment
    between runs, which it does.

    Ties break toward the latest turn: with signal equal, the more recent
    engagement is the one still fresh when the question arrives.
    """
    return (SIGNAL_RANK.get(moment.signal, UNRANKED), -moment.turn)


def select[M: Rankable](moments: Sequence[M]) -> M | None:
    """The one moment worth a question, or None if there were none.

    Generic, not `Rankable -> Rankable`. Ranking reads exactly two attributes and
    `Rankable` says so, but the caller gets its own type back: `triage` selects a
    `Moment` and immediately reads `.topic` and `.shows` off it, which a bare
    `Rankable` return would forbid. The protocol's narrowness is the design; the
    type variable is what keeps it from leaking onto the caller.
    """
    if not moments:
        return None
    return min(moments, key=rank_key)
