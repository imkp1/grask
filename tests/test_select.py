"""Tests for stage 1 selection.

Selection exists as its own module because it is the part of stage 1 that must
be reproducible. Measurement showed the LLM finds the same moments run to run
but picks among them arbitrarily; these tests pin the picking.
"""

from __future__ import annotations

from dataclasses import dataclass

from grill.select import SIGNAL_RANK, rank_key, select


@dataclass
class FakeMoment:
    signal: str
    turn: int


class TestSelect:
    def test_returns_none_for_no_moments(self):
        assert select([]) is None

    def test_returns_the_only_moment(self):
        only = FakeMoment("pushed_back", 3)
        assert select([only]) is only

    def test_prefers_asked_why_over_every_other_signal(self):
        moments = [
            FakeMoment("explained_at_length", 9),
            FakeMoment("pushed_back", 8),
            FakeMoment("new_pattern", 7),
            FakeMoment("asked_why", 1),
        ]
        assert select(moments).signal == "asked_why"

    def test_prefers_quote_provable_signals_over_code_grounded_ones(self):
        # The divergence from the design doc's stated order, pinned: a signal a
        # quote can prove outranks one it cannot.
        moments = [FakeMoment("new_pattern", 9), FakeMoment("pushed_back", 2)]
        assert select(moments).signal == "pushed_back"

    def test_breaks_ties_on_the_latest_turn(self):
        moments = [FakeMoment("asked_why", 2), FakeMoment("asked_why", 11)]
        assert select(moments).turn == 11

    def test_is_independent_of_input_order(self):
        a = FakeMoment("asked_why", 4)
        b = FakeMoment("pushed_back", 9)
        assert select([a, b]) is select([b, a]) is a

    def test_adding_a_lower_ranked_moment_never_changes_the_winner(self):
        # Extraction wobbles at the margin: a moment present on one run and
        # absent on the next must not disturb a winner that outranks it, or the
        # wobble propagates into the topic.
        core = [FakeMoment("asked_why", 5), FakeMoment("pushed_back", 12)]
        winner = select(core)
        marginal = FakeMoment("new_pattern", 20)
        assert select([*core, marginal]) is winner

    def test_unknown_signals_rank_last_rather_than_crashing(self):
        moments = [FakeMoment("vibes", 9), FakeMoment("explained_at_length", 1)]
        assert select(moments).signal == "explained_at_length"

    def test_every_valid_signal_has_a_rank(self):
        from grill.triage import VALID_SIGNALS

        assert set(SIGNAL_RANK) == set(VALID_SIGNALS)

    def test_rank_key_orders_by_signal_then_latest_turn(self):
        assert rank_key(FakeMoment("asked_why", 3)) < rank_key(FakeMoment("pushed_back", 3))
        assert rank_key(FakeMoment("asked_why", 9)) < rank_key(FakeMoment("asked_why", 3))
