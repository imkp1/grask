"""Tests for the corpus capture runner.

Only `plan` is worth pinning down: it decides what gets spent on, and every one
of its inputs is injected, so the whole selection policy is testable without
touching a transcript or a model. The pool and the report around it are plumbing.

The filter that matters most is the ordering one. The limit has to apply after
the empty-session check, or a corpus that is 70% agent scratchpad turns
`--limit 40` into four real sessions and thirty-six no-ops.
"""

from __future__ import annotations

from pathlib import Path

from grill.capture_run import DEFAULT_EXCLUDE, plan


def transcript(project: str, session: str) -> Path:
    return Path(f"/corpus/{project}/{session}.jsonl")


NOTHING_CAPTURED = lambda _sid: False  # noqa: E731
ALL_HAVE_TURNS = lambda _path: True  # noqa: E731


def test_excludes_grill_and_scratchpad_projects_by_default():
    result = plan(
        [
            transcript("-Users-me-projects-grill", "a"),
            transcript("-private-tmp-claude-501-...-scratchpad", "b"),
            transcript("-Users-me-projects-other-work", "c"),
        ],
        exclude=DEFAULT_EXCLUDE,
        is_captured=NOTHING_CAPTURED,
        has_human_turns=ALL_HAVE_TURNS,
        limit=None,
    )

    assert [p.stem for p in result.selected] == ["c"]
    assert result.excluded == 2


def test_a_docs_subdirectory_of_grill_is_still_grill():
    # The corpus really contains `-Users-...-projects-grill-docs-design-...`,
    # which is grill's own work under a name that does not end in "grill".
    result = plan(
        [transcript("-Users-me-projects-grill-docs-design-2026-07-21", "a")],
        exclude=DEFAULT_EXCLUDE,
        is_captured=NOTHING_CAPTURED,
        has_human_turns=ALL_HAVE_TURNS,
        limit=None,
    )

    assert result.selected == []
    assert result.excluded == 1


def test_skips_sessions_already_in_the_db():
    result = plan(
        [transcript("proj", "old"), transcript("proj", "new")],
        exclude=(),
        is_captured=lambda sid: sid == "old",
        has_human_turns=ALL_HAVE_TURNS,
        limit=None,
    )

    assert [p.stem for p in result.selected] == ["new"]
    assert result.already_captured == 1


def test_the_limit_counts_only_sessions_that_would_reach_triage():
    # Two empties between the real ones. A limit applied before the human-turn
    # check would return one session here instead of two.
    result = plan(
        [
            transcript("proj", "empty1"),
            transcript("proj", "real1"),
            transcript("proj", "empty2"),
            transcript("proj", "real2"),
            transcript("proj", "real3"),
        ],
        exclude=(),
        is_captured=NOTHING_CAPTURED,
        has_human_turns=lambda p: p.stem.startswith("real"),
        limit=2,
    )

    assert [p.stem for p in result.selected] == ["real1", "real2"]
    assert result.no_human_turns == 2
    assert result.over_limit == 1


def test_an_unparseable_transcript_is_dropped_not_raised(tmp_path: Path):
    from grill.capture_run import has_human_turns

    assert has_human_turns(tmp_path / "does-not-exist.jsonl") is False


def selection_of(n: int):
    return plan(
        [transcript("proj", str(i)) for i in range(n)],
        exclude=(),
        is_captured=NOTHING_CAPTURED,
        has_human_turns=ALL_HAVE_TURNS,
        limit=None,
    )


def test_estimate_scales_with_the_selection():
    assert selection_of(0).estimate_usd() == 0.0
    # Ten sessions of a real corpus should be single-digit dollars, not a surprise.
    assert 1.0 < selection_of(10).estimate_usd() < 5.0


def test_the_quoted_range_brackets_both_observed_batches():
    # The two runs this was calibrated from. A point estimate fit to n=14 quoted
    # $9 for the 50-session batch, which then cost $14.74 — the range exists so
    # the number the developer authorises is not the optimistic one.
    low, high = selection_of(50).estimate_range_usd()
    assert low <= 14.74 <= high

    low, high = selection_of(10).estimate_range_usd()
    assert low <= 1.62 <= high


def test_probe_count_is_estimated_from_the_kept_rate():
    assert selection_of(50).estimate_probes() == 21  # observed: 22
    assert selection_of(0).estimate_probes() == 0
