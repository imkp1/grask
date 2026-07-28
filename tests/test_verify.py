"""Tests for stage 4, the check that makes a verdict trustworthy.

No LLM is called here. What these pin down is the one property the whole stage
exists for: a probe survives only when an independent judgment says exactly one
option is true AND names the stored key. Every other shape of judgment — two
true, none true, one true that is not the key — is a probe grask throws away.

The backtest that justified this stage measured 47 stored probes: 44 clean, 3
where the judge named a non-key option, 0 where it found two true. The one
arbitration path the design originally called for — repointing `correct_idx` at
the option the judge preferred — is deliberately absent, because on that sample
it would have been right once and would have installed a false key the other
time. Discarding is what a 6% defect rate buys you cheaply; repointing is what
turns a verifier into a second source of the bug it exists to catch.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from grask.llm import Completion, LLMError
from grask.probe import Probe, Rubric
from grask.verify import (
    MAX_ATTEMPTS,
    ProbeUnverified,
    build_prompt,
    parse_verdicts,
    verify,
)

PROBE = Probe(
    question="What supplies the mutual exclusion in `mkdir \"$dir/run.lock\"`?",
    options=(
        "The shell's `mkdir` builtin holds a process-wide advisory lock",
        "`mkdir` sets the sticky bit, preventing another entry of the same name",
        "The kernel serialises directory-entry creation within the parent directory",
        "`mkdir` is a single syscall, so it cannot be interrupted partway",
    ),
    correct_idx=2,
    explanation="Directory namespace uniqueness is enforced under the parent inode lock.",
    rubric=Rubric(topic="lock files", hypothesis="They took mkdir's atomicity on faith."),
    cost_usd=0.22,
)


def judgment(*verdicts: bool, reason: str = "because") -> str:
    """A check-B response marking the given options true."""
    return json.dumps(
        [{"index": i, "true": v, "reason": reason} for i, v in enumerate(verdicts)]
    )


def as_written(probe: Probe) -> Probe:
    """The verified probe with stage 4's meter reset, for comparing to `PROBE`.

    Verification adds to `cost_usd` and `duration_ms` and must leave everything
    else alone; resetting both is how "everything else" gets asserted.
    """
    return replace(probe, cost_usd=PROBE.cost_usd, duration_ms=PROBE.duration_ms)


def replies(*texts: str, cost: float = 0.04):
    """A `complete` stub that returns each text in turn."""
    queue = list(texts)

    def stub(prompt: str) -> Completion:
        stub.prompts.append(prompt)
        return Completion(text=queue.pop(0), cost_usd=cost, duration_ms=10)

    stub.prompts = []
    return stub


class TestParseVerdicts:
    def test_reads_one_verdict_per_option(self):
        parsed = parse_verdicts(judgment(False, False, True, False), 4)
        assert [v.true for v in parsed] == [False, False, True, False]

    def test_falls_back_to_position_when_index_is_missing(self):
        text = json.dumps([{"true": True, "reason": "r"}, {"true": False, "reason": "r"}])
        assert [v.index for v in parse_verdicts(text, 2)] == [0, 1]

    def test_falls_back_to_position_when_the_indices_are_not_a_permutation(self):
        # Every element in range and every element the same. Read literally this
        # moves the true verdict onto option 0 and discards a probe for
        # disagreeing with a key it never disagreed with.
        text = json.dumps([{"index": 0, "true": i == 2, "reason": "r"} for i in range(4)])
        parsed = parse_verdicts(text, 4)
        assert [v.index for v in parsed] == [0, 1, 2, 3]
        assert [v.index for v in parsed if v.true] == [2]

    def test_keeps_a_permutation_the_model_wrote_out_of_order(self):
        text = json.dumps(
            [{"index": i, "true": i == 3, "reason": "r"} for i in (2, 0, 3, 1)]
        )
        assert [v.index for v in parse_verdicts(text, 4) if v.true] == [3]

    def test_rejects_a_judgment_that_skips_an_option(self):
        with pytest.raises(LLMError):
            parse_verdicts(judgment(True, False), 4)

    def test_rejects_a_verdict_with_no_reason(self):
        text = json.dumps([{"index": i, "true": False, "reason": ""} for i in range(3)])
        with pytest.raises(LLMError):
            parse_verdicts(text, 3)

    def test_rejects_unparseable_text(self):
        with pytest.raises(LLMError):
            parse_verdicts("I'll go and check the files first.", 4)


class TestPrompt:
    def test_shows_every_option(self):
        prompt = build_prompt(PROBE)
        for option in PROBE.options:
            assert option in prompt

    def test_never_reveals_the_key_or_the_reasoning_behind_it(self):
        # A judge that has seen why the answer is the answer is the model
        # agreeing with itself. The key is an int, so "not in the prompt" is not
        # a testable claim about it — what is testable is that nothing marks an
        # option, and that the explanation never appears.
        prompt = build_prompt(PROBE)
        assert PROBE.explanation not in prompt
        assert PROBE.rubric.hypothesis not in prompt
        marked = prompt.split(PROBE.options[PROBE.correct_idx])[1].splitlines()[0]
        assert marked.strip() == ""

    def test_tells_the_judge_it_has_no_files(self):
        # Two of 47 backtested probes came back as an attempted tool call rather
        # than JSON: the judge went looking for the repo. Saying it is not there
        # is what fixed both.
        assert "no tools" in build_prompt(PROBE).lower()


class TestVerify:
    def test_a_probe_whose_key_is_the_only_true_option_survives(self):
        verified = verify(PROBE, complete=replies(judgment(False, False, True, False)))
        assert as_written(verified) == PROBE

    def test_two_true_options_are_unverified(self):
        with pytest.raises(ProbeUnverified, match="2 options"):
            verify(PROBE, complete=replies(judgment(False, False, True, True)))

    def test_no_true_option_is_unverified(self):
        with pytest.raises(ProbeUnverified, match="no option"):
            verify(PROBE, complete=replies(judgment(False, False, False, False)))

    def test_one_true_option_that_is_not_the_key_is_unverified(self):
        # Never repointed. See the module docstring.
        with pytest.raises(ProbeUnverified, match="option 3"):
            verify(PROBE, complete=replies(judgment(False, False, False, True)))

    def test_the_reason_reaches_the_exception(self):
        text = judgment(False, False, True, True, reason="both describe real serialisation")
        with pytest.raises(ProbeUnverified, match="both describe real serialisation"):
            verify(PROBE, complete=replies(text))

    def test_verification_cost_is_added_to_the_probe(self):
        verified = verify(PROBE, complete=replies(judgment(False, False, True, False), cost=0.04))
        assert verified.cost_usd == pytest.approx(0.26)

    def test_verification_duration_is_added_to_the_probe(self):
        # Cost and duration have to cover the same stages. Two columns on one
        # row that counted different halves of the pipeline are two numbers
        # nobody can put beside each other.
        verified = verify(PROBE, complete=replies(judgment(False, False, True, False)))
        assert verified.duration_ms == (PROBE.duration_ms or 0) + 10

    def test_a_discarded_probe_reports_what_it_cost(self):
        # No probes row survives this path, so the exception is the only thing
        # that ever knows what stages 3 and 4 spent to reach the judgment.
        with pytest.raises(ProbeUnverified) as caught:
            verify(PROBE, complete=replies(judgment(False, False, False, False), cost=0.04))

        assert caught.value.cost_usd == pytest.approx(0.26)
        assert caught.value.duration_ms == 10

    def test_an_unparseable_judgment_is_retried(self):
        stub = replies("not json at all", judgment(False, False, True, False))
        verified = verify(PROBE, complete=stub)
        assert as_written(verified) == PROBE
        assert len(stub.prompts) == 2

    def test_cost_counts_the_attempts_that_failed(self):
        stub = replies("not json at all", judgment(False, False, True, False), cost=0.04)
        assert verify(PROBE, complete=stub).cost_usd == pytest.approx(0.30)

    def test_a_call_failure_raises_rather_than_discarding(self):
        # The design's rule: only a judgment discards a probe. A broken CLI must
        # not quietly empty the queue, so this is an LLMError and not the
        # ProbeUnverified that capture treats as "throw the probe away".
        def broken(prompt: str) -> Completion:
            raise LLMError("claude exited 1")

        with pytest.raises(LLMError) as caught:
            verify(PROBE, complete=broken)
        assert not isinstance(caught.value, ProbeUnverified)

    def test_gives_up_after_the_attempt_budget(self):
        stub = replies(*["still not json"] * MAX_ATTEMPTS)
        with pytest.raises(LLMError):
            verify(PROBE, complete=stub)
        assert len(stub.prompts) == MAX_ATTEMPTS

    def test_a_judgment_is_never_retried(self):
        # A retry here would be resampling until the probe passes, which is the
        # verifier agreeing with the generator by persistence.
        stub = replies(judgment(False, False, False, False), judgment(False, False, True, False))
        with pytest.raises(ProbeUnverified):
            verify(PROBE, complete=stub)
        assert len(stub.prompts) == 1
