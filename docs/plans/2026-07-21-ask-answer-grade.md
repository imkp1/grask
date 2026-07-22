# Ask / answer / grade — the `grill` CLI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop from a stored probe to a graded verdict — `grill` takes one pending question, interrogates the developer to a decision, and records what happened.

**Architecture:** `cli.py` owns the terminal and nothing else. `ask.py` is a pure loop over an injected console, judge, and follow-up writer, so the whole interrogation is drivable in tests at zero model spend. `judge.py` moves from one boolean to a per-criterion breakdown with `passed` derived from it. `storage.py` gains three additive tables; nothing existing changes.

**Tech Stack:** Python 3.12, stdlib only, SQLite via `sqlite3`, pytest. No dependencies — the project has none and gains none.

**Spec:** `docs/superpowers/specs/2026-07-21-ask-answer-grade-design.md`. Read it before Task 1. This plan implements it; it does not restate its reasoning.

## Global Constraints

- **Python 3.12, stdlib only.** `dependencies = []` in `pyproject.toml` stays empty. `pytest` stays in the dev group.
- **`from __future__ import annotations`** at the top of every module, matching every existing file.
- **Frozen dataclasses** for all domain types, matching `Seed`, `Probe`, `Verdict`, `Rubric`.
- **Tuples, not lists**, for every fixed collection on a dataclass. The codebase is consistent about this and storage round-trip tests assert it.
- **Model calls happen in exactly two modules**: `judge.py` and `probe.py`, both through `grill.llm.complete`. `ask.py`, `cli.py`, and `storage.py` never call a model directly.
- **Never invent a grade.** An unparseable or failed judge call raises `LLMError` and becomes outcome `error`. It never degrades to a pass. This inverts `capture.py`'s never-raise rule on purpose: the developer is present.
- **Tests that call a real model are marked `@pytest.mark.calibration`** and are deselected by default via `addopts = "-m 'not calibration'"`. Any test that spends money and is not marked is a bug.
- **`GRILL_HOME` is redirected for every test** by the autouse fixture in `tests/conftest.py`. Do not remove it, and do not write a test that assumes the real `~/.claude/grill/`.
- **Outcome vocabulary, exact strings:** `passed`, `failed`, `skipped`, `premise_rejected`, `error`. These are written to a TEXT column and queried by hand later; a typo is a silently-wrong measurement.
- **Confidence vocabulary, exact values:** `95`, `70`, `40`. Stored as INTEGER, NULL when the interrogation ended before an answer.
- **Commit after every task.** Conventional-commit prefixes, matching the existing log (`feat:`, `test:`, `docs:`).

## Task order and why

`judge.py` is rewritten first and its calibration gate runs second, before anything depends on the new `Verdict`. That ordering is deliberate: Task 2 is the only task in this plan that can fail in a way that invalidates the design. If 45/45 does not reproduce, stop and re-open the spec rather than building five tasks on top of a judge that got worse.

```
Task 1  judge.py per-criterion rewrite        (no deps)
Task 2  the calibration gate — 45/45 or stop  (deps: 1)   ← merge condition
Task 3  probe.followup()                      (no deps)
Task 4  ask.py — the interrogation loop       (deps: 1, 3)
Task 5  storage.py — three tables + queries   (deps: 4, for its types)
Task 6  cli.py + pyproject wiring             (deps: 4, 5)
Task 7  end-to-end measurement                (deps: all)
```

`ask.py` owns both `PendingProbe` and `Interrogation`, and `storage.py` imports them. That is the direction the codebase already runs — `storage.py` imports `Probe` from `probe.py` and `Seed` from `seed.py` — and it is also the only direction that does not create an import cycle.

---

### Task 1: `judge.py` — per-criterion grading

`Verdict` gains a per-criterion breakdown and `passed` becomes derived. One judge, one prompt.

**Files:**
- Modify: `src/grill/judge.py` — `Verdict` (38-49), `VERDICT_KEYS` (21), `PROMPT` (52-117), `build_prompt` (120-128), `parse_verdict` (131-146), `judge` (149-165)
- Test: `tests/test_judge.py` (create — there is no unit test file for the judge today, only `tests/test_judge_calibration.py`)

**Interfaces:**
- Consumes: `grill.llm.complete`, `extract_json_object`, `LLMError`; `Rubric` (unchanged).
- Produces:
  - `CriterionResult(criterion: str, met: bool)` — frozen dataclass
  - `Verdict(criteria: tuple[CriterionResult, ...], reasoning: str, cost_usd: float | None = None, duration_ms: int | None = None)` with `passed: bool` as a property
  - `parse_verdict(text: str, rubric: Rubric) -> Verdict` — **signature changed**, now takes the rubric
  - `judge(*, rubric: Rubric, probe: str, answer: str) -> Verdict` — signature unchanged
  - `VERDICT_KEYS = ("criteria", "reasoning")`

**Design note the implementer needs.** The model returns `{"n": 1, "met": true}` objects — a number and a boolean, no free text. The criterion *text* on `CriterionResult` comes from `rubric.criteria[n-1]`, never from the model's echo. Two reasons: the model cannot mangle text it never writes, and `criterion_results` rows in Task 5 then carry canonical text, which is what makes "which criteria fail most often" a real query rather than a fuzzy group-by.

**Known regression, accepted.** `salvage_flat_object` recovers flat shapes only, so it cannot recover the `criteria` array when unescaped quotes break the JSON. Previously an unescaped quote in `reasoning` was survivable; now it costs the whole verdict, which becomes outcome `error`. This is the correct direction to be wrong — the alternative is guessing a grade — and it is cheap here because the developer is sitting in front of it and can re-run `grill`. Say so in the `parse_verdict` docstring.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_judge.py`:

```python
"""Unit tests for the judge's parsing and its derived pass.

No model is called here. What these pin down is the one property the whole
component rests on: `passed` is `all(met)` and nothing else can produce a pass.
An empty criteria tuple in particular must not pass — `all(())` is True, and a
vacuous pass is the coward failure arriving through a hole in a parser instead
of through the prompt.
"""

from __future__ import annotations

import json

import pytest

from grill.judge import CriterionResult, Rubric, Verdict, build_prompt, parse_verdict
from grill.llm import LLMError

RUBRIC = Rubric(
    topic="idempotency of the retry path",
    hypothesis="the developer accepted the key without knowing what it dedupes against",
    criteria=(
        "Says the key must be stable across retries.",
        "Does not claim a fresh UUID per attempt would work.",
    ),
)


def response(**overrides) -> str:
    body = {
        "criteria": [{"n": 1, "met": True}, {"n": 2, "met": True}],
        "reasoning": "You named the stability requirement directly.",
    }
    body.update(overrides)
    return json.dumps(body)


class TestPassedIsDerived:
    def test_all_met_passes(self):
        verdict = parse_verdict(response(), RUBRIC)

        assert verdict.passed is True

    def test_one_unmet_fails(self):
        verdict = parse_verdict(
            response(criteria=[{"n": 1, "met": True}, {"n": 2, "met": False}]), RUBRIC
        )

        assert verdict.passed is False

    def test_no_criteria_is_not_a_pass(self):
        """`all(())` is True. A verdict over nothing must still be a fail."""
        assert Verdict(criteria=(), reasoning="").passed is False


class TestCriterionText:
    def test_text_comes_from_the_rubric_not_the_model(self):
        """The model returns indices; we own the words.

        A criterion the model paraphrased would be a criterion no query can
        group by, which is the whole reason `criterion_results` is a table.
        """
        verdict = parse_verdict(response(), RUBRIC)

        assert verdict.criteria == (
            CriterionResult(criterion=RUBRIC.criteria[0], met=True),
            CriterionResult(criterion=RUBRIC.criteria[1], met=True),
        )


class TestRefusesToGuess:
    def test_rejects_a_short_breakdown(self):
        with pytest.raises(LLMError):
            parse_verdict(response(criteria=[{"n": 1, "met": True}]), RUBRIC)

    def test_rejects_a_non_boolean_met(self):
        with pytest.raises(LLMError):
            parse_verdict(
                response(criteria=[{"n": 1, "met": "yes"}, {"n": 2, "met": True}]), RUBRIC
            )

    def test_rejects_out_of_order_indices(self):
        """Order is how a grade is attached to a criterion. Scrambled is unusable."""
        with pytest.raises(LLMError):
            parse_verdict(
                response(criteria=[{"n": 2, "met": True}, {"n": 1, "met": False}]), RUBRIC
            )

    def test_missing_reasoning_is_empty_not_fatal(self):
        """Reasoning is for the developer, not for the decision. Its absence is survivable."""
        verdict = parse_verdict(json.dumps({"criteria": [{"n": 1, "met": True}, {"n": 2, "met": True}]}), RUBRIC)

        assert verdict.reasoning == ""
        assert verdict.passed is True


class TestPrompt:
    def test_numbers_the_criteria(self):
        """The model answers by index, so the index has to be visible to it."""
        prompt = build_prompt(RUBRIC, "why is the key stable?", "because it comes from the order id")

        assert "1. Says the key must be stable across retries." in prompt
        assert "2. Does not claim a fresh UUID per attempt would work." in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_judge.py -v`
Expected: FAIL — `ImportError: cannot import name 'CriterionResult' from 'grill.judge'`

- [ ] **Step 3: Replace `VERDICT_KEYS` and `Verdict`**

In `src/grill/judge.py`, replace line 21:

```python
VERDICT_KEYS = ("criteria", "reasoning")
```

Replace the `Verdict` dataclass (lines 38-49) with:

```python
@dataclass(frozen=True)
class CriterionResult:
    """One criterion, and whether the answer contained what it names."""

    criterion: str
    met: bool


@dataclass(frozen=True)
class Verdict:
    """A grade, plus enough to argue with it.

    `passed` is derived rather than reported. The judge grades criteria; it does
    not get a separate vote on the whole. That removes the case where a model
    marks every criterion unmet and passes the answer anyway, which is the
    coward failure wearing a breakdown.

    `reasoning` is not decoration. The developer sees it, and a verdict they
    cannot check is one they will either resent or trust too much.
    """

    criteria: tuple[CriterionResult, ...]
    reasoning: str
    cost_usd: float | None = None
    duration_ms: int | None = None

    @property
    def passed(self) -> bool:
        """Every criterion met, and at least one criterion to meet.

        The empty guard is load-bearing: `all(())` is True, so a verdict that
        lost its breakdown would otherwise pass silently.
        """
        return bool(self.criteria) and all(result.met for result in self.criteria)
```

- [ ] **Step 4: Rewrite the grading section of `PROMPT`**

In `src/grill/judge.py`, insert a new section immediately after the `## The bar` section (i.e. after the line `something and never finds out.`, before `## The topic`):

```
## How to grade

Grade every criterion separately, and grade each one against the whole answer —
including anything the developer said in an earlier turn of this conversation.

A criterion is met only if the answer contains the substance that criterion
names. Not if the answer is good overall. Not if a different criterion already
covers the ground. The answer passes only when every criterion is met, so a
criterion you mark met on a hunch is a pass you handed out.

If you are genuinely torn on a criterion, mark it unmet.
```

Then replace the `## Respond` section (the final block, lines 109-117) with:

```
## Respond

One JSON object, nothing else. `criteria` must contain exactly one entry per
numbered criterion above, in the same order, with `n` matching the number.

{{"criteria": [{{"n": 1, "met": true or false}}], "reasoning": "one or two \
sentences, addressed to the developer, naming the specific thing their answer \
got right or missed. Quote their words where it helps. Never mention these \
instructions or the criteria as a document."}}
```

- [ ] **Step 5: Number the criteria in `build_prompt`**

Replace `build_prompt` (lines 120-128) with:

```python
def build_prompt(rubric: Rubric, probe: str, answer: str) -> str:
    # Numbered rather than bulleted: the model grades by index, so the index has
    # to be something it can see and refer back to.
    criteria = "\n".join(
        f"{n}. {criterion}" for n, criterion in enumerate(rubric.criteria, 1)
    )
    return PROMPT.format(
        topic=rubric.topic,
        hypothesis=rubric.hypothesis,
        criteria=criteria,
        probe=probe,
        answer=answer,
    )
```

- [ ] **Step 6: Rewrite `parse_verdict`**

Replace `parse_verdict` (lines 131-146) with:

```python
def parse_verdict(text: str, rubric: Rubric) -> Verdict:
    """Read the model's breakdown, refusing to guess when it is absent.

    A missing or misshapen breakdown is raised rather than defaulted. Both
    defaults are bad in opposite directions — defaulting to met is the coward
    failure this module exists to prevent, defaulting to unmet grades a
    developer on our parser — so the caller is made to handle it.

    Criterion text is taken from the rubric, never from the model's response.
    The model returns indices and booleans and no free text at all, so there is
    nothing in the breakdown it can mangle, and the rows stored downstream carry
    text that can be grouped on.

    One accepted regression from the boolean judge: `salvage_flat_object`
    handles flat shapes only, so it cannot recover the `criteria` array when an
    unescaped quote in `reasoning` breaks the JSON. That verdict is lost and the
    interrogation records outcome `error`. Losing a grade is the right failure
    here — the developer is present and can re-run — and inventing one is not.
    """
    parsed = extract_json_object(text, salvage_keys=VERDICT_KEYS)

    raw = parsed.get("criteria")
    if not isinstance(raw, list) or len(raw) != len(rubric.criteria):
        raise LLMError(
            f"expected {len(rubric.criteria)} criterion grades, got {raw!r}: {text[:400]}"
        )

    results = []
    for position, (criterion, entry) in enumerate(zip(rubric.criteria, raw), 1):
        if not isinstance(entry, dict):
            raise LLMError(f"criterion grade {position} is not an object: {text[:400]}")

        # Order carries the whole mapping from grade to criterion, so a wrong or
        # missing `n` means we do not know what was graded.
        if entry.get("n") != position:
            raise LLMError(
                f"criterion grade {position} is out of order (n={entry.get('n')!r}): {text[:400]}"
            )

        met = entry.get("met")
        if not isinstance(met, bool):
            raise LLMError(f"no boolean 'met' for criterion {position}: {text[:400]}")

        results.append(CriterionResult(criterion=criterion, met=met))

    reasoning = parsed.get("reasoning")
    return Verdict(
        criteria=tuple(results),
        reasoning=reasoning if isinstance(reasoning, str) else "",
    )
```

- [ ] **Step 7: Update `judge` to pass the rubric through**

Replace the body of `judge` (lines 158-165) with:

```python
    completion = complete(build_prompt(rubric, probe, answer))
    verdict = parse_verdict(completion.text, rubric)
    return Verdict(
        criteria=verdict.criteria,
        reasoning=verdict.reasoning,
        cost_usd=completion.cost_usd,
        duration_ms=completion.duration_ms,
    )
```

- [ ] **Step 8: Update the existing calibration assertions**

`tests/test_judge_calibration.py` asserts `verdict.passed is False/True`. Those still work — `passed` is a property. Add per-criterion assertions to the two failing fixtures so the breakdown is checked, not just the roll-up. Append to `tests/test_judge_calibration.py`:

```python
def test_fluent_nonsense_fails_the_criterion_it_actually_violates():
    """A fail is only useful if it fails for the right reason.

    FLUENT_NONSENSE claims the saving comes from concurrency and caching, which
    is criterion 3 verbatim. If the roll-up fails but criterion 3 is marked met,
    the judge got the right answer from the wrong reading, and the per-criterion
    data every downstream query depends on is noise.
    """
    verdict = judge(rubric=RUBRIC, probe=PROBE, answer=FLUENT_NONSENSE)

    by_text = {result.criterion: result.met for result in verdict.criteria}
    assert by_text[RUBRIC.criteria[2]] is False, verdict.reasoning


def test_a_solid_answer_meets_every_criterion():
    """The pass has to come from the breakdown, not around it.

    `passed` is `all(met)`, so this is close to a tautology on the roll-up — but
    it is not one on the data. A solid answer scored as met-met-met is what says
    the criteria are individually satisfiable; a pass built on criteria the judge
    marked met without cause would make `criterion_results` unreadable.
    """
    verdict = judge(rubric=RUBRIC, probe=PROBE, answer=SOLID)

    assert all(result.met for result in verdict.criteria), verdict.reasoning
    assert len(verdict.criteria) == len(RUBRIC.criteria)


def test_idk_meets_nothing():
    """An answer that asserts nothing cannot satisfy a criterion.

    This is the floor for per-criterion grading: if any criterion comes back met
    on an explicit "I didn't check", the grader is pattern-matching on topic
    words rather than on substance.
    """
    verdict = judge(rubric=RUBRIC, probe=PROBE, answer=HONEST_IDK)

    assert not any(result.met for result in verdict.criteria), verdict.reasoning
```

- [ ] **Step 9: Run the full offline suite**

Run: `.venv/bin/pytest -v`
Expected: PASS — the 125 existing tests plus the new `tests/test_judge.py`. Nothing outside `judge.py` imports `Verdict`, so nothing else should break. If something does, fix the caller rather than reintroducing a `passed` field.

- [ ] **Step 10: Commit**

```bash
git add src/grill/judge.py tests/test_judge.py tests/test_judge_calibration.py
git commit -m "feat: the judge grades each criterion, and passed is derived from all of them"
```

---

### Task 2: The calibration gate — 45/45 or stop

**This is the merge condition for the whole plan.** The judge was measured at 45/45 in `docs/measurements/2026-07-21-generated-rubric-calibration.md`, and Task 1 rewrote it. Reasoning cannot settle which direction accuracy moved.

**The runner does not exist.** The measurement doc lists its artifacts as `generated_probes.json`, `answers.py`, `calibration_results.json` — inputs and outputs, no harness. Only `run_boundary.py` (a different, 15-call partial-answer run) was committed. So the re-run has to be rebuilt from the committed inputs first. Cost is unaffected: the probes cost $2.38 to generate and are on disk, so this run is judge calls only, ~$1.07.

This is the third instance of the pattern saved as `controls-without-recovery-paths`: a documented control whose cost assumed machinery nobody built. Writing the runner as a committed artifact is what retires it here.

**Files:**
- Create: `docs/measurements/2026-07-21-generated-rubric-calibration/run_calibration.py`
- Create: `docs/measurements/2026-07-22-per-criterion-judge-recalibration.md` (the write-up; date it the day you run it)
- Modify: `docs/measurements/2026-07-21-generated-rubric-calibration.md` — one line under **Artifacts** naming the runner

**Interfaces:**
- Consumes: `judge(rubric=..., probe=..., answer=...) -> Verdict` with `.criteria`, `.passed`, `.reasoning`, `.cost_usd` from Task 1.
- Produces: nothing code depends on. A number, and a go/no-go.

- [ ] **Step 1: Write the runner**

Create `docs/measurements/2026-07-21-generated-rubric-calibration/run_calibration.py`:

```python
"""The 45-call sweep, rebuilt so it can be re-run against a changed judge.

The original run of this measurement was scripted ad hoc and only its inputs and
outputs were committed. That made "45/45 on re-run" a merge condition nobody
could actually execute. This file is the harness; the inputs beside it are
unchanged, so a re-run measures the judge and nothing else.

Run from this directory:

    cd docs/measurements/2026-07-21-generated-rubric-calibration
    ../../../.venv/bin/python run_calibration.py

5 sessions x 3 answer types x 3 repeats = 45 judge calls, about $1.07.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from answers import ANSWERS, EXPECTED

from grill.judge import Rubric, judge

PROBES = Path("generated_probes.json")
OUT = Path("calibration_results_per_criterion.json")
REPEATS = 3


def main() -> None:
    probes = json.loads(PROBES.read_text())
    records = []
    correct = Counter()
    total = Counter()

    for session_id, by_type in ANSWERS.items():
        entry = probes[session_id]
        rubric = Rubric(
            topic=entry["seed"]["topic"],
            hypothesis=entry["seed"]["hypothesis"],
            criteria=tuple(entry["probe"]["criteria"]),
        )
        question = entry["probe"]["question"]

        for answer_type, answer in by_type.items():
            expected = EXPECTED[answer_type]
            marks = []
            for _ in range(REPEATS):
                verdict = judge(rubric=rubric, probe=question, answer=answer)
                is_correct = verdict.passed is expected
                marks.append("." if is_correct else "X")
                correct[answer_type] += int(is_correct)
                total[answer_type] += 1
                records.append(
                    {
                        "session_id": session_id,
                        "signal": entry["signal"],
                        "answer_type": answer_type,
                        "expected_pass": expected,
                        "passed": verdict.passed,
                        "correct": is_correct,
                        # The new column. The roll-up reproducing at 45/45 while
                        # the breakdown is arbitrary would be a false green:
                        # every downstream query reads the criteria, not passed.
                        "criteria": [
                            {"criterion": c.criterion, "met": c.met} for c in verdict.criteria
                        ],
                        "reasoning": verdict.reasoning,
                        "cost_usd": verdict.cost_usd,
                    }
                )
            print(f"{session_id[:8]}  {answer_type:<16} [{''.join(marks)}]")

    OUT.write_text(json.dumps(records, indent=2))

    print()
    for answer_type in EXPECTED:
        print(f"{answer_type:<16} {correct[answer_type]}/{total[answer_type]}")
    print(f"\nTOTAL {sum(correct.values())}/{sum(total.values())}")
    print(f"judge cost: ${sum(r['cost_usd'] or 0 for r in records):.4f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

```bash
cd docs/measurements/2026-07-21-generated-rubric-calibration
../../../.venv/bin/python run_calibration.py
```

Expected: `TOTAL 45/45` and a cost near $1.07.

**If it is not 45/45: stop.** Do not proceed to Task 3, and do not "fix" it by loosening a test. Record what failed and which direction it moved — stricter (a `solid` answer failed on one criterion) or more lenient (a `fluent_nonsense` passed) — and re-open the spec. The spec names this as the one real risk in the design, and a partial reproduction is the signal it was written for.

- [ ] **Step 3: Compare the breakdown against the roll-up**

Read `calibration_results_per_criterion.json`. Beyond the count, check the thing the count cannot show:

- On every `fluent_nonsense`, at least one criterion is unmet **and** it is a criterion the answer actually violates.
- On every `honest_idk`, no criterion is met.
- On every `solid`, all criteria are met — not "enough of them".

A 45/45 with an incoherent breakdown is a fail for this plan's purposes even though the number reproduced, because Tasks 4 and 5 route follow-ups and stored rows off the breakdown, not off `passed`.

- [ ] **Step 4: Write the measurement up**

Create `docs/measurements/2026-07-22-per-criterion-judge-recalibration.md`, following the structure of the existing calibration docs. It must state:

- The judge under measurement (`src/grill/judge.py` at the Task 1 commit SHA).
- The count, per answer type and total.
- Measured cost, from the runner's own output — not this plan's estimate.
- Which direction any disagreement went, if any.
- What the breakdown check in Step 3 found.
- Explicitly: whether the boundary caveat from the previous doc (partial answers untested) is still open. It is — nothing in this task touches it. Do not let a clean re-run imply otherwise.

- [ ] **Step 5: Note the runner in the original doc**

In `docs/measurements/2026-07-21-generated-rubric-calibration.md`, line 9, extend the **Artifacts** line to name `run_calibration.py` and record that it was reconstructed on 2026-07-22 rather than at the time of the original run. A reader must not conclude the original number came from this script.

- [ ] **Step 6: Commit**

```bash
git add docs/measurements/
git commit -m "test: the 45-call gate, rebuilt as an artifact and re-run against the per-criterion judge"
```

---

### Task 3: `probe.followup()` — the targeted second question

**Files:**
- Modify: `src/grill/probe.py` — add `Followup`, `FOLLOWUP_PROMPT`, `parse_followup`, `followup`; extract `reject_if_compound` from `parse_probe` (202-208)
- Test: `tests/test_probe.py` — add a `TestFollowup` class

**Interfaces:**
- Consumes: `grill.llm.complete`, `LLMError`, `Completion`; `ProbeRejected`, `SECOND_QUESTION`, `MAX_QUESTION_MARKS`, `MAX_ATTEMPTS`, `CORRECTION` (all already in `probe.py`).
- Produces:
  - `Followup(question: str, cost_usd: float | None = None, duration_ms: int | None = None)` — frozen dataclass
  - `followup(question: str, answer: str, unmet_criterion: str, *, complete=complete, attempts: int = MAX_ATTEMPTS) -> Followup`
  - `reject_if_compound(question: str) -> None` — raises `ProbeRejected`

**Deliberate deviation from the spec.** The spec writes the signature as `-> str`. It returns `Followup` instead, because the same spec's `asks.cost_usd` column is defined as "judge + follow-up calls summed" and a bare string cannot carry a cost. This is the smallest change that makes both statements true.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_probe.py`:

```python
class TestFollowup:
    """The follow-up inherits stage 3's rules because it is stage 3's job.

    A second module writing questions would be a second place to keep the
    one-question rule correct, and that rule has already been broken once here.
    """

    def followup_response(self, question: str) -> str:
        return json.dumps({"question": question})

    def test_asks_one_question(self):
        calls = []

        def complete(prompt):
            calls.append(prompt)
            return completion(
                self.followup_response("Stable across what, specifically?")
            )

        result = followup(
            "What would break if the key were regenerated on each retry?",
            "It would stop deduping.",
            "Says the key must be derived from something stable across retries.",
            complete=complete,
        )

        assert result.question == "Stable across what, specifically?"
        assert result.cost_usd == 0.01

    def test_rejects_a_compound_followup(self):
        """The same net as `parse_probe`, because it is the same promise."""
        responses = iter(
            [
                self.followup_response("Stable across what, and how would you check?"),
                self.followup_response("Stable across what, specifically?"),
            ]
        )

        result = followup(
            "What would break if the key were regenerated?",
            "It would stop deduping.",
            "Says the key must be derived from something stable across retries.",
            complete=lambda prompt: completion(next(responses)),
        )

        assert result.question == "Stable across what, specifically?"

    def test_gives_up_after_the_attempt_budget(self):
        calls = []

        def complete(prompt):
            calls.append(prompt)
            return completion(
                self.followup_response("Stable across what, and how would you check?")
            )

        with pytest.raises(ProbeRejected):
            followup(
                "What would break?",
                "It would stop deduping.",
                "Says the key must be stable across retries.",
                complete=complete,
            )

        assert len(calls) == MAX_ATTEMPTS

    def test_the_prompt_carries_the_unmet_criterion_and_the_answer(self):
        calls = []

        def complete(prompt):
            calls.append(prompt)
            return completion(self.followup_response("Stable across what?"))

        followup(
            "What would break if the key were regenerated?",
            "It would stop deduping.",
            "Says the key must be derived from something stable across retries.",
            complete=complete,
        )

        assert "stable across retries" in calls[0]
        assert "It would stop deduping." in calls[0]

    def test_the_prompt_forbids_leaking_the_criterion(self):
        """A follow-up that states the criterion it tests grades itself."""
        calls = []

        def complete(prompt):
            calls.append(prompt)
            return completion(self.followup_response("Stable across what?"))

        followup("What would break?", "It stops deduping.", "Says the key must be stable.", complete=complete)

        assert "not contain its own answer" in calls[0]
```

Add to the imports at the top of `tests/test_probe.py`:

```python
from grill.probe import (
    MAX_ATTEMPTS,
    ProbeRejected,
    build_prompt,
    followup,
    parse_probe,
    probe,
)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_probe.py -v -k Followup`
Expected: FAIL — `ImportError: cannot import name 'followup' from 'grill.probe'`

- [ ] **Step 3: Extract the shared structural check**

In `src/grill/probe.py`, add above `parse_probe`:

```python
def reject_if_compound(question: str) -> None:
    """The one-question rule, shared by the probe and its follow-ups.

    Extracted rather than duplicated: the promise a follow-up makes is the same
    twenty-second promise the first question made, and two copies of this rule
    is two places for it to drift.
    """
    if question.count("?") > MAX_QUESTION_MARKS:
        raise ProbeRejected(f"probe asks more than one question: {question!r}", question)

    if SECOND_QUESTION.search(question):
        raise ProbeRejected(
            f"probe asks a second question after a conjunction: {question!r}", question
        )
```

Then in `parse_probe`, replace lines 202-208 (the two `if` blocks) with:

```python
    reject_if_compound(question)
```

- [ ] **Step 4: Add the follow-up prompt and type**

In `src/grill/probe.py`, add after the `Probe` dataclass:

```python
@dataclass(frozen=True)
class Followup:
    """One targeted second question, and what it cost to write.

    A bare string would be simpler, but `asks.cost_usd` is defined as judge plus
    follow-up calls summed, and a string cannot carry the second half of that.
    """

    question: str
    cost_usd: float | None = None
    duration_ms: int | None = None


FOLLOWUP_PROMPT = """\
You are stage 3 of `grill`, writing a follow-up. The developer was asked one
question and answered it. Their answer satisfied some of what a real answer must
show, but not this:

{unmet_criterion}

## The question they were asked

{question}

## What they have said so far

{answer}

## What to produce

ONE follow-up question that gives them a second chance at the specific thing
above, answerable out loud in about twenty seconds.

The question must:
- aim at the gap, not at the topic again. They already answered the broad
  question; asking it a second time in different words wastes their turn.
- not contain its own answer. If the thing you are testing is visible in the
  question, you have tested nothing and they will pass by reading it back.
- not tell them what they got wrong, or that they got anything wrong. You are
  asking, not grading. The judge does the grading and the developer sees its
  reasoning at the end.
- be one question. No "and". No "also". No parts.
- stay in their terms. Use the words they used where you can.

## Respond

One JSON object, nothing else:

{{"question": "..."}}
"""


def build_followup_prompt(question: str, answer: str, unmet_criterion: str) -> str:
    return FOLLOWUP_PROMPT.format(
        question=question,
        answer=answer,
        unmet_criterion=unmet_criterion,
    )


def parse_followup(completion: Completion) -> Followup:
    """Read a follow-up, holding it to the probe's structural rules."""
    parsed = extract_json_object(completion.text, salvage_keys=("question",))

    question = parsed.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ProbeRejected("follow-up has no question")
    question = question.strip()

    reject_if_compound(question)

    return Followup(
        question=question,
        cost_usd=completion.cost_usd,
        duration_ms=completion.duration_ms,
    )
```

- [ ] **Step 5: Add `followup`**

Add at the end of `src/grill/probe.py`:

```python
def followup(
    question: str,
    answer: str,
    unmet_criterion: str,
    *,
    complete=complete,
    attempts: int = MAX_ATTEMPTS,
) -> Followup:
    """Write the second question, aimed at the one thing the answer missed.

    Retried on the same terms as `probe`, and for the same reason: the rejection
    it guards against is stochastic, so resampling is the appropriate response.
    A rejection goes back with the offending question attached; a call failure
    goes back unchanged.

    Every attempt is billed, so every attempt is in the returned cost.
    """
    base = build_followup_prompt(question, answer, unmet_criterion)
    prompt = base
    spent = 0.0
    last: LLMError | None = None

    for _ in range(attempts):
        try:
            completion = complete(prompt)
        except LLMError as exc:
            last = exc
            prompt = base
            continue

        spent += completion.cost_usd or 0.0

        try:
            parsed = parse_followup(completion)
        except ProbeRejected as exc:
            last = exc
            prompt = base + CORRECTION.format(question=exc.question, reason=exc.reason)
            continue

        return replace(parsed, cost_usd=spent)

    raise last if last else LLMError("follow-up exhausted its attempts without an error")
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_probe.py -v`
Expected: PASS — the new `TestFollowup` class and every pre-existing probe test, including `test_rejects_a_multi_part_question` and `test_rejects_two_questions_joined_by_a_conjunction`, which now run through the extracted `reject_if_compound`.

- [ ] **Step 7: Commit**

```bash
git add src/grill/probe.py tests/test_probe.py
git commit -m "feat: probe writes a follow-up aimed at the criterion the answer missed"
```

---

### Task 4: `ask.py` — the interrogation

Pure logic, no terminal, no argparse, no model call of its own. Everything that costs money is injected.

**Files:**
- Create: `src/grill/ask.py`
- Test: `tests/test_ask.py`

**Interfaces:**
- Consumes: `Rubric`, `Verdict`, `CriterionResult` from Task 1; `followup`, `Followup` from Task 3; `LLMError`.
- Produces:
  - `PendingProbe(probe_id: int, question: str, rubric: Rubric, created_at: str)` — frozen dataclass; the input to an interrogation. Storage constructs it in Task 5.
  - `AnswerTurn(turn: int, question: str, answer: str, criteria: tuple[CriterionResult, ...])`
  - `Interrogation(probe_id: int, outcome: str, confidence: int | None, objection: str | None, turns: tuple[AnswerTurn, ...], cost_usd: float | None, reasoning: str)`
  - `Console` — `typing.Protocol` with `show(text: str) -> None` and `prompt(text: str) -> str`
  - `ask(pending: PendingProbe, console: Console, *, judge=judge, followup=followup) -> Interrogation`
  - Constants: `PASSED`, `FAILED`, `SKIPPED`, `PREMISE_REJECTED`, `ERROR`, `CONFIDENCES = (95, 70, 40)`, `WRONG = "/wrong"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ask.py`:

```python
"""Tests for the interrogation loop.

A scripted console and injected grading stand in for the terminal and the model,
so the entire loop — every exit, every bound — is exercised at zero spend. That
is the reason `ask.py` takes both as arguments rather than reaching for them.

What these pin down is mostly the exits. The happy path is one branch; skip,
`/wrong`, exhaustion, and a failed model call are four, and three of them are
the ones a developer actually hits on a Tuesday.
"""

from __future__ import annotations

import pytest

from grill.ask import (
    ERROR,
    FAILED,
    PASSED,
    PREMISE_REJECTED,
    SKIPPED,
    PendingProbe,
    ask,
)
from grill.judge import CriterionResult, Rubric, Verdict
from grill.llm import LLMError
from grill.probe import Followup

RUBRIC = Rubric(
    topic="idempotency of the retry path",
    hypothesis="the developer accepted the key without knowing what it dedupes against",
    criteria=(
        "Says the key must be stable across retries.",
        "Does not claim a fresh UUID per attempt would work.",
    ),
)

PENDING = PendingProbe(
    probe_id=7,
    question="What would break if the idempotency key were regenerated on each retry?",
    rubric=RUBRIC,
    created_at="2026-07-21T09:00:00+00:00",
)


class ScriptedConsole:
    """Replays a list of inputs and remembers everything it was shown."""

    def __init__(self, inputs: list[str]) -> None:
        self.inputs = list(inputs)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def show(self, text: str) -> None:
        self.shown.append(text)

    def prompt(self, text: str) -> str:
        self.prompts.append(text)
        if not self.inputs:
            raise AssertionError(f"console ran out of scripted input at: {text!r}")
        return self.inputs.pop(0)


def verdict(*met: bool, reasoning: str = "because.") -> Verdict:
    return Verdict(
        criteria=tuple(
            CriterionResult(criterion=c, met=m) for c, m in zip(RUBRIC.criteria, met)
        ),
        reasoning=reasoning,
        cost_usd=0.02,
    )


def grading(*verdicts: Verdict):
    """A judge that returns the given verdicts in order, recording its inputs."""
    calls = []
    remaining = list(verdicts)

    def judge(*, rubric, probe, answer):
        calls.append({"rubric": rubric, "probe": probe, "answer": answer})
        return remaining.pop(0)

    judge.calls = calls
    return judge


def writing(*questions: str):
    calls = []
    remaining = list(questions)

    def followup(question, answer, unmet_criterion):
        calls.append(
            {"question": question, "answer": answer, "unmet": unmet_criterion}
        )
        return Followup(question=remaining.pop(0), cost_usd=0.01)

    followup.calls = calls
    return followup


class TestPassing:
    def test_passes_on_the_first_turn(self):
        console = ScriptedConsole(["95", "it has to be stable across retries"])

        result = ask(PENDING, console, judge=grading(verdict(True, True)), followup=writing())

        assert result.outcome == PASSED
        assert result.confidence == 95
        assert len(result.turns) == 1
        assert result.turns[0].question == PENDING.question

    def test_shows_the_reasoning(self):
        console = ScriptedConsole(["70", "stable across retries"])

        ask(
            PENDING,
            console,
            judge=grading(verdict(True, True, reasoning="You named the stability requirement.")),
            followup=writing(),
        )

        assert any("You named the stability requirement." in text for text in console.shown)

    def test_the_context_line_carries_the_topic(self):
        """The developer has to know which piece of work this is about first."""
        console = ScriptedConsole(["95", "stable across retries"])

        ask(PENDING, console, judge=grading(verdict(True, True)), followup=writing())

        assert any("idempotency of the retry path" in text for text in console.shown)


class TestFollowups:
    def test_passes_after_one_followup_aimed_at_the_unmet_criterion(self):
        console = ScriptedConsole(["95", "it dedupes", "it comes from the order id"])
        judge = grading(verdict(False, True), verdict(True, True))
        followup = writing("Where does the key come from?")

        result = ask(PENDING, console, judge=judge, followup=followup)

        assert result.outcome == PASSED
        assert len(result.turns) == 2
        assert followup.calls[0]["unmet"] == RUBRIC.criteria[0]
        assert result.turns[1].question == "Where does the key come from?"

    def test_the_followup_targets_the_first_unmet_criterion(self):
        console = ScriptedConsole(["95", "it dedupes", "from the order id"])
        judge = grading(verdict(True, False), verdict(True, True))
        followup = writing("What about a fresh UUID?")

        ask(PENDING, console, judge=judge, followup=followup)

        assert followup.calls[0]["unmet"] == RUBRIC.criteria[1]

    def test_judging_accumulates_every_answer(self):
        """Grading the latest turn alone would make the loop unable to terminate on merit.

        A narrow follow-up gets a narrow answer, which would mark already-met
        criteria unmet the moment the developer cooperates.
        """
        console = ScriptedConsole(["95", "it dedupes", "from the order id"])
        judge = grading(verdict(False, True), verdict(True, True))

        ask(PENDING, console, judge=judge, followup=writing("Where does it come from?"))

        assert judge.calls[1]["answer"] == "it dedupes\n\nfrom the order id"

    def test_judging_always_uses_the_original_question(self):
        console = ScriptedConsole(["95", "it dedupes", "from the order id"])
        judge = grading(verdict(False, True), verdict(True, True))

        ask(PENDING, console, judge=judge, followup=writing("Where does it come from?"))

        assert judge.calls[1]["probe"] == PENDING.question


class TestBounds:
    def test_turns_are_capped_at_the_number_of_criteria(self):
        console = ScriptedConsole(["40", "one", "two", "three"])
        judge = grading(verdict(False, False), verdict(False, False))
        followup = writing("second?", "third?")

        result = ask(PENDING, console, judge=judge, followup=followup)

        assert result.outcome == FAILED
        assert len(result.turns) == len(RUBRIC.criteria)
        assert len(judge.calls) == len(RUBRIC.criteria)

    def test_a_failure_still_shows_the_reasoning(self):
        console = ScriptedConsole(["40", "one", "two"])
        judge = grading(
            verdict(False, False), verdict(False, False, reasoning="Neither point landed.")
        )

        result = ask(PENDING, console, judge=judge, followup=writing("again?"))

        assert result.outcome == FAILED
        assert any("Neither point landed." in text for text in console.shown)


class TestSkip:
    def test_skip_at_the_confidence_prompt(self):
        console = ScriptedConsole([""])
        judge = grading()

        result = ask(PENDING, console, judge=judge, followup=writing())

        assert result.outcome == SKIPPED
        assert result.confidence is None
        assert result.turns == ()
        assert judge.calls == []

    def test_skip_at_the_answer_prompt(self):
        console = ScriptedConsole(["95", ""])
        judge = grading()

        result = ask(PENDING, console, judge=judge, followup=writing())

        assert result.outcome == SKIPPED
        assert result.confidence == 95
        assert judge.calls == []

    def test_skip_at_a_followup_keeps_the_turn_already_paid_for(self):
        console = ScriptedConsole(["95", "it dedupes", ""])
        judge = grading(verdict(False, True))

        result = ask(PENDING, console, judge=judge, followup=writing("Where from?"))

        assert result.outcome == SKIPPED
        assert len(result.turns) == 1


class TestWrong:
    def test_wrong_at_the_confidence_prompt_with_an_objection(self):
        console = ScriptedConsole(["/wrong", "I wrote that key myself, it was never the agent's"])
        judge = grading()

        result = ask(PENDING, console, judge=judge, followup=writing())

        assert result.outcome == PREMISE_REJECTED
        assert result.objection == "I wrote that key myself, it was never the agent's"
        assert result.confidence is None
        assert judge.calls == []

    def test_wrong_without_an_objection(self):
        console = ScriptedConsole(["/wrong", ""])

        result = ask(PENDING, console, judge=grading(), followup=writing())

        assert result.outcome == PREMISE_REJECTED
        assert result.objection is None

    def test_wrong_at_the_answer_prompt(self):
        console = ScriptedConsole(["70", "/wrong", "the question misreads the diff"])

        result = ask(PENDING, console, judge=grading(), followup=writing())

        assert result.outcome == PREMISE_REJECTED
        assert result.confidence == 70

    def test_wrong_spends_nothing(self):
        console = ScriptedConsole(["/wrong", ""])

        result = ask(PENDING, console, judge=grading(), followup=writing())

        assert result.cost_usd == 0.0


class TestErrors:
    def test_a_failed_judge_call_is_an_error_not_a_pass(self):
        console = ScriptedConsole(["95", "it dedupes"])

        def judge(*, rubric, probe, answer):
            raise LLMError("claude exited 1")

        result = ask(PENDING, console, judge=judge, followup=writing())

        assert result.outcome == ERROR
        assert result.outcome != PASSED

    def test_a_failed_judge_call_says_so_plainly(self):
        console = ScriptedConsole(["95", "it dedupes"])

        def judge(*, rubric, probe, answer):
            raise LLMError("claude exited 1")

        ask(PENDING, console, judge=judge, followup=writing())

        assert any("claude exited 1" in text for text in console.shown)

    def test_a_failed_followup_ends_the_interrogation_as_an_error(self):
        console = ScriptedConsole(["95", "it dedupes"])

        def followup(question, answer, unmet_criterion):
            raise LLMError("claude timed out after 180s")

        result = ask(PENDING, console, judge=grading(verdict(False, True)), followup=followup)

        assert result.outcome == ERROR
        assert len(result.turns) == 1


class TestConfidence:
    @pytest.mark.parametrize("typed,expected", [("95", 95), ("70", 70), ("40", 40)])
    def test_accepts_the_three_values(self, typed, expected):
        console = ScriptedConsole([typed, "stable across retries"])

        result = ask(PENDING, console, judge=grading(verdict(True, True)), followup=writing())

        assert result.confidence == expected

    def test_reprompts_on_anything_else(self):
        console = ScriptedConsole(["100", "95", "stable across retries"])

        result = ask(PENDING, console, judge=grading(verdict(True, True)), followup=writing())

        assert result.confidence == 95
        assert len(console.prompts) == 3

    def test_confidence_is_asked_once_and_never_again(self):
        """Asking again at turn three is friction with a payoff that does not exist yet."""
        console = ScriptedConsole(["95", "it dedupes", "from the order id"])

        ask(
            PENDING,
            console,
            judge=grading(verdict(False, True), verdict(True, True)),
            followup=writing("Where from?"),
        )

        assert sum("confidence" in p.lower() for p in console.prompts) == 1


class TestCost:
    def test_sums_the_judge_and_followup_calls(self):
        console = ScriptedConsole(["95", "it dedupes", "from the order id"])
        judge = grading(verdict(False, True), verdict(True, True))

        result = ask(PENDING, console, judge=judge, followup=writing("Where from?"))

        # two judge calls at 0.02, one follow-up at 0.01
        assert result.cost_usd == pytest.approx(0.05)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ask.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grill.ask'`

- [ ] **Step 3: Write `ask.py`**

Create `src/grill/ask.py`:

```python
"""One probe, interrogated to a verdict.

Pure logic. The console, the judge, and the follow-up writer are all injected,
exactly as `capture_session` injects its stages, so the whole loop is drivable by
a scripted console at zero model spend. No argparse, no terminal control codes,
no TTY — those belong to `cli.py`, and keeping them out is what leaves the
delivery question open. `ask` does not care whether a human typed `grill` or a
hook called it.

Errors invert capture's rule. `capture_session` never raises because it runs
detached with nothing watching; here the developer is sitting in front of it, and
inventing a grade for them is worse than admitting the call failed. A model
failure becomes outcome `error` and is shown plainly. It never degrades to a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from grill.judge import CriterionResult, Rubric, Verdict
from grill.judge import judge as _judge
from grill.llm import LLMError
from grill.probe import followup as _followup

# Written to a TEXT column and grouped on by hand later, so the strings are the
# schema. `premise_rejected` is its own outcome rather than a flavour of skip:
# it is the zealot rate, and a rate you cannot query is a rate nobody checks.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
PREMISE_REJECTED = "premise_rejected"
ERROR = "error"

# Three coarse buckets, not a slider. The number exists to be quoted back at
# resurfacing — "you said 95%" — and a 0-100 field invites a hedge.
CONFIDENCES = (95, 70, 40)

WRONG = "/wrong"


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
    """

    probe_id: int
    question: str
    rubric: Rubric
    created_at: str


@dataclass(frozen=True)
class AnswerTurn:
    """One developer turn, the question that prompted it, and how it graded."""

    turn: int
    question: str
    answer: str
    criteria: tuple[CriterionResult, ...]


@dataclass(frozen=True)
class Interrogation:
    """Everything that happened, ready to be stored. No database in sight."""

    probe_id: int
    outcome: str
    confidence: int | None
    objection: str | None
    turns: tuple[AnswerTurn, ...]
    cost_usd: float | None
    reasoning: str


CONFIDENCE_PROMPT = "confidence   [95 | 70 | 40]   ·   enter = skip   ·   /wrong"
ANSWER_PROMPT = "answer                        ·   enter = skip   ·   /wrong"
OBJECTION_PROMPT = "what's wrong with it? (enter to skip)"
CONFIDENCE_HINT = "type 95, 70, or 40."


def context_line(pending: PendingProbe) -> str:
    """One line of orientation above the question.

    Without it the developer reads a question about work they cannot place, which
    is the version of this tool that feels like a quiz. `created_at` is the
    probe's, which is the session's within a few seconds of it.
    """
    when = pending.created_at[:10]
    return f"from {when} · {pending.rubric.topic}"


def _first_unmet(verdict: Verdict) -> CriterionResult | None:
    return next((result for result in verdict.criteria if not result.met), None)


def ask(
    pending: PendingProbe,
    console: Console,
    *,
    judge=_judge,
    followup=_followup,
) -> Interrogation:
    """Run one interrogation to a verdict.

    Developer turns are capped at `len(pending.rubric.criteria)`, which stage 3
    constrains to 2-4, so the worst case is three follow-ups. The cap is the
    criteria count rather than a constant because every follow-up exists to
    retest exactly one unmet criterion; once they are exhausted there is nothing
    left to ask that was written down before the answer existed.
    """
    spent = 0.0
    turns: list[AnswerTurn] = []

    def done(outcome: str, *, confidence=None, objection=None, reasoning="") -> Interrogation:
        return Interrogation(
            probe_id=pending.probe_id,
            outcome=outcome,
            confidence=confidence,
            objection=objection,
            turns=tuple(turns),
            cost_usd=spent,
            reasoning=reasoning,
        )

    def ask_objection() -> str | None:
        """Shared `/wrong` handling: prompt once for an optional reason.

        Optional because requiring an argument to escape is how you get an escape
        hatch nobody uses. The outcome is the signal; the text is a bonus.
        """
        typed = console.prompt(OBJECTION_PROMPT).strip()
        return typed or None

    console.show(context_line(pending))
    console.show(pending.question)

    # Confidence, once, before any answer. Committing a number before answering
    # is the whole mechanism; asking again later is friction with no payoff.
    confidence: int | None = None
    while confidence is None:
        typed = console.prompt(CONFIDENCE_PROMPT).strip()
        if not typed:
            return done(SKIPPED)
        if typed == WRONG:
            return done(PREMISE_REJECTED, objection=ask_objection())
        if typed.isdigit() and int(typed) in CONFIDENCES:
            confidence = int(typed)
        else:
            console.show(CONFIDENCE_HINT)

    question = pending.question
    answers: list[str] = []
    last_reasoning = ""

    for turn in range(len(pending.rubric.criteria)):
        typed = console.prompt(ANSWER_PROMPT).strip()
        if not typed:
            return done(SKIPPED, confidence=confidence, reasoning=last_reasoning)
        if typed == WRONG:
            return done(
                PREMISE_REJECTED, confidence=confidence, objection=ask_objection()
            )

        answers.append(typed)

        # Accumulated, not latest-only. A narrow follow-up gets a narrow answer,
        # and grading that alone would mark already-met criteria unmet the moment
        # the developer cooperates — the loop could then never terminate on merit.
        try:
            verdict = judge(
                rubric=pending.rubric,
                probe=pending.question,
                answer="\n\n".join(answers),
            )
        except LLMError as exc:
            console.show(f"the judge call failed: {exc}")
            return done(ERROR, confidence=confidence)

        spent += verdict.cost_usd or 0.0
        last_reasoning = verdict.reasoning
        turns.append(
            AnswerTurn(
                turn=turn,
                question=question,
                answer=typed,
                criteria=verdict.criteria,
            )
        )

        if verdict.passed:
            console.show(verdict.reasoning)
            return done(PASSED, confidence=confidence, reasoning=verdict.reasoning)

        if turn == len(pending.rubric.criteria) - 1:
            console.show(verdict.reasoning)
            return done(FAILED, confidence=confidence, reasoning=verdict.reasoning)

        unmet = _first_unmet(verdict)
        if unmet is None:  # pragma: no cover - `passed` already covers this
            console.show(verdict.reasoning)
            return done(FAILED, confidence=confidence, reasoning=verdict.reasoning)

        try:
            # The writer gets the question just asked and everything said so far,
            # so it can aim at the gap without re-covering ground already walked.
            written = followup(question, "\n\n".join(answers), unmet.criterion)
        except LLMError as exc:
            console.show(f"could not write a follow-up: {exc}")
            return done(ERROR, confidence=confidence, reasoning=last_reasoning)

        spent += written.cost_usd or 0.0
        question = written.question
        console.show(question)

    # Unreachable: the loop returns on its final iteration.
    return done(FAILED, confidence=confidence, reasoning=last_reasoning)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_ask.py -v`
Expected: PASS — all of `TestPassing`, `TestFollowups`, `TestBounds`, `TestSkip`, `TestWrong`, `TestErrors`, `TestConfidence`, `TestCost`.

- [ ] **Step 5: Run the whole offline suite**

Run: `.venv/bin/pytest`
Expected: PASS, no calibration tests selected.

- [ ] **Step 6: Commit**

```bash
git add src/grill/ask.py tests/test_ask.py
git commit -m "feat: the interrogation loop, with both free exits and a bounded follow-up chain"
```

---

### Task 5: `storage.py` — three additive tables

Nothing existing changes. `CREATE TABLE IF NOT EXISTS` runs on every open, so the new tables appear in the live db on the next `grill` without a migration step.

**Files:**
- Modify: `src/grill/storage.py` — extend `SCHEMA` (24-59), add `PROBE_TTL_DAYS`, `next_probe`, `record_ask`
- Test: `tests/test_storage.py` — add `TestNextProbe` and `TestRecordAsk`

**Interfaces:**
- Consumes: `PendingProbe`, `Interrogation`, `AnswerTurn` from Task 4; `Rubric` from `judge.py`.
- Produces:
  - `Store.next_probe() -> PendingProbe | None`
  - `Store.record_ask(interrogation: Interrogation) -> int` (returns the `asks` row id)
  - `PROBE_TTL_DAYS = 7`

**Design notes the implementer needs.**

- **Expiry is computed, not stored.** The cutoff is `now - 7 days` as an ISO-8601 string, compared lexicographically against `probes.created_at`. That comparison is valid because `_now()` always produces `datetime.now(timezone.utc).isoformat()` — same offset, same field widths, so string order is time order. Do not add a status column, and do not add a background sweep.
- **"Unasked" is the absence of an `asks` row**, via `LEFT JOIN ... WHERE asks.id IS NULL`. The `UNIQUE` constraint on `probe_id` is what makes that a correct query rather than a convention, so it is not optional.
- **Newest first**, tie-broken by `p.id DESC`. Two probes written in the same second are otherwise ordered arbitrarily, which makes the "newest" test flaky.
- **`record_ask` writes all three tables in one transaction.** A committed `asks` row with no `answers` rows would consume the probe and lose the interrogation.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py`:

```python
class TestNextProbe:
    """Selection is newest-unasked-unexpired, and all three words are load-bearing."""

    def stored(self, store: Store, *, session_id: str, created_at: str, topic: str) -> int:
        """Write a session, seed, and probe, forcing `probes.created_at`."""
        store.record_session(
            session_id=session_id,
            transcript_path=f"/tmp/{session_id}.jsonl",
            cwd="/tmp",
            git_branch="main",
            verdict="ask",
        )
        seed_id = store.add_seed(replace(a_seed(session_id), topic=topic))
        probe_id = store.add_probe(seed_id, a_probe())
        store.conn.execute(
            "UPDATE probes SET created_at = ? WHERE id = ?", (created_at, probe_id)
        )
        store.conn.commit()
        return probe_id

    def test_returns_none_on_an_empty_store(self, store: Store):
        assert store.next_probe() is None

    def test_returns_the_newest(self, store: Store):
        self.stored(store, session_id="old", created_at=iso_days_ago(3), topic="the old one")
        newest = self.stored(
            store, session_id="new", created_at=iso_days_ago(1), topic="the new one"
        )

        pending = store.next_probe()

        assert pending is not None
        assert pending.probe_id == newest
        assert pending.rubric.topic == "the new one"

    def test_skips_a_probe_that_was_already_asked(self, store: Store):
        older = self.stored(store, session_id="old", created_at=iso_days_ago(3), topic="older")
        newer = self.stored(store, session_id="new", created_at=iso_days_ago(1), topic="newer")
        store.record_ask(an_interrogation(probe_id=newer))

        pending = store.next_probe()

        assert pending is not None
        assert pending.probe_id == older

    def test_skips_a_probe_older_than_the_ttl(self, store: Store):
        self.stored(
            store,
            session_id="stale",
            created_at=iso_days_ago(PROBE_TTL_DAYS + 1),
            topic="stale",
        )

        assert store.next_probe() is None

    def test_keeps_a_probe_inside_the_ttl(self, store: Store):
        self.stored(
            store,
            session_id="fresh",
            created_at=iso_days_ago(PROBE_TTL_DAYS - 1),
            topic="fresh",
        )

        assert store.next_probe() is not None

    def test_carries_the_whole_rubric(self, store: Store):
        """The judge needs topic and hypothesis, and neither lives on `probes`."""
        self.stored(store, session_id="one", created_at=iso_days_ago(1), topic="the topic")

        pending = store.next_probe()

        assert pending is not None
        assert pending.rubric.topic == "the topic"
        assert pending.rubric.hypothesis == a_seed().hypothesis
        assert pending.rubric.criteria == a_probe().rubric.criteria
        assert pending.question == a_probe().question


class TestRecordAsk:
    def a_probe_id(self, store: Store) -> int:
        store.record_session(
            session_id="0198e4f1",
            transcript_path="/tmp/0198e4f1.jsonl",
            cwd="/tmp",
            git_branch="main",
            verdict="ask",
        )
        return store.add_probe(store.add_seed(a_seed()), a_probe())

    def test_round_trips_an_interrogation(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(an_interrogation(probe_id=probe_id))

        row = store.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        assert row["outcome"] == "passed"
        assert row["confidence"] == 95
        assert row["turns"] == 1
        assert row["cost_usd"] == 0.02
        assert row["completed_at"] is not None

    def test_stores_a_row_per_turn_with_its_question(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(an_interrogation(probe_id=probe_id, turn_count=2))

        rows = store.conn.execute(
            "SELECT * FROM answers WHERE ask_id = ? ORDER BY turn", (ask_id,)
        ).fetchall()
        assert [r["turn"] for r in rows] == [0, 1]
        assert rows[1]["question"] == "follow-up 1"

    def test_stores_a_row_per_criterion_per_turn(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(an_interrogation(probe_id=probe_id, turn_count=2))

        rows = store.conn.execute(
            "SELECT cr.criterion, cr.met FROM criterion_results cr"
            " JOIN answers a ON a.id = cr.answer_id WHERE a.ask_id = ?",
            (ask_id,),
        ).fetchall()
        assert len(rows) == 4  # two turns x two criteria
        assert {r["criterion"] for r in rows} == set(a_probe().rubric.criteria)

    def test_one_ask_per_probe(self, store: Store):
        """The UNIQUE constraint is what makes 'unasked = no asks row' true."""
        probe_id = self.a_probe_id(store)
        store.record_ask(an_interrogation(probe_id=probe_id))

        with pytest.raises(sqlite3.IntegrityError):
            store.record_ask(an_interrogation(probe_id=probe_id))

    def test_a_skip_records_no_answers_and_a_null_confidence(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(
            Interrogation(
                probe_id=probe_id,
                outcome="skipped",
                confidence=None,
                objection=None,
                turns=(),
                cost_usd=0.0,
                reasoning="",
            )
        )

        row = store.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        assert row["confidence"] is None
        assert row["turns"] == 0
        assert store.conn.execute(
            "SELECT count(*) c FROM answers WHERE ask_id = ?", (ask_id,)
        ).fetchone()["c"] == 0

    def test_an_objection_survives(self, store: Store):
        probe_id = self.a_probe_id(store)

        ask_id = store.record_ask(
            Interrogation(
                probe_id=probe_id,
                outcome="premise_rejected",
                confidence=None,
                objection="the question misreads the diff",
                turns=(),
                cost_usd=0.0,
                reasoning="",
            )
        )

        row = store.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        assert row["objection"] == "the question misreads the diff"
```

Add to the top of `tests/test_storage.py`. The existing `from grill.storage import Store, grill_home` on line 20 is **extended** rather than duplicated — add `PROBE_TTL_DAYS` to it. The rest are new lines:

```python
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from grill.ask import AnswerTurn, Interrogation
from grill.judge import CriterionResult
from grill.storage import PROBE_TTL_DAYS, Store, grill_home  # extended, not added


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def an_interrogation(*, probe_id: int, turn_count: int = 1) -> Interrogation:
    criteria = a_probe().rubric.criteria
    turns = tuple(
        AnswerTurn(
            turn=n,
            question=a_probe().question if n == 0 else f"follow-up {n}",
            answer=f"answer {n}",
            criteria=tuple(CriterionResult(criterion=c, met=True) for c in criteria),
        )
        for n in range(turn_count)
    )
    return Interrogation(
        probe_id=probe_id,
        outcome="passed",
        confidence=95,
        objection=None,
        turns=turns,
        cost_usd=0.02,
        reasoning="You named it.",
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_storage.py -v`
Expected: FAIL — `ImportError: cannot import name 'PROBE_TTL_DAYS' from 'grill.storage'`

- [ ] **Step 3: Extend the schema**

In `src/grill/storage.py`, append to the `SCHEMA` string (after the `probes` table, before the closing `"""`):

```sql

CREATE TABLE IF NOT EXISTS asks (
    id           INTEGER PRIMARY KEY,
    probe_id     INTEGER NOT NULL UNIQUE REFERENCES probes(id),
    asked_at     TEXT NOT NULL,
    confidence   INTEGER,
    outcome      TEXT NOT NULL,
    objection    TEXT,
    turns        INTEGER NOT NULL,
    cost_usd     REAL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS answers (
    id         INTEGER PRIMARY KEY,
    ask_id     INTEGER NOT NULL REFERENCES asks(id),
    turn       INTEGER NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS criterion_results (
    id        INTEGER PRIMARY KEY,
    answer_id INTEGER NOT NULL REFERENCES answers(id),
    criterion TEXT NOT NULL,
    met       INTEGER NOT NULL
);
```

Update the module docstring's "Three tables" to say six, and add a sentence: the last three record what was asked and how it graded, and `criterion_results` is a table rather than a JSON column because "which criteria fail most often" is the query that says whether stage 3 writes gradeable criteria at all — the inverse of why `quotes` and `refs` are JSON.

- [ ] **Step 4: Add the constant and the imports**

At the top of `src/grill/storage.py`, add to the imports:

```python
from datetime import datetime, timedelta, timezone

from grill.ask import Interrogation, PendingProbe
from grill.judge import Rubric
```

And below `SCHEMA`:

```python
# A probe about work you did last week is a quiz, not a question. Seven days is
# the outer edge of "you still remember writing this". Expiry is computed at
# query time rather than stored, so nothing has to sweep and no lifecycle column
# can fall out of sync with the clock.
PROBE_TTL_DAYS = 7
```

- [ ] **Step 5: Add `next_probe`**

Add to `Store`:

```python
    def next_probe(self) -> PendingProbe | None:
        """The newest unasked, unexpired probe. One per invocation.

        Newest rather than oldest because the value is a question about the code
        you shipped this afternoon; oldest-first leads with the session you have
        most thoroughly forgotten. One at a time because the product is one
        question, not a queue.

        The rubric is reassembled from the seed's topic and hypothesis plus the
        probe's criteria — `add_probe` deliberately does not duplicate the first
        two, so this join is where they come back together.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PROBE_TTL_DAYS)).isoformat()
        row = self.conn.execute(
            "SELECT p.id, p.question, p.criteria, p.created_at, s.topic, s.hypothesis"
            " FROM probes p"
            " JOIN seeds s ON s.id = p.seed_id"
            " LEFT JOIN asks a ON a.probe_id = p.id"
            # No asks row is what 'unasked' means. UNIQUE(probe_id) is what makes
            # that correct rather than merely usually true.
            " WHERE a.id IS NULL AND p.created_at >= ?"
            # id DESC breaks the tie between two probes written in the same second.
            " ORDER BY p.created_at DESC, p.id DESC"
            " LIMIT 1",
            (cutoff,),
        ).fetchone()

        if row is None:
            return None

        return PendingProbe(
            probe_id=int(row["id"]),
            question=row["question"],
            rubric=Rubric(
                topic=row["topic"],
                hypothesis=row["hypothesis"],
                criteria=tuple(json.loads(row["criteria"])),
            ),
            created_at=row["created_at"],
        )
```

- [ ] **Step 6: Add `record_ask`**

Add to `Store`:

```python
    def record_ask(self, interrogation: Interrogation) -> int:
        """Persist one interrogation across all three tables, or none of them.

        A single transaction because a committed `asks` row with no `answers`
        rows would consume the probe — UNIQUE(probe_id) means it can never be
        asked again — while losing what the developer actually said.
        """
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO asks"
                " (probe_id, asked_at, confidence, outcome, objection, turns, cost_usd,"
                "  completed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    interrogation.probe_id,
                    now,
                    interrogation.confidence,
                    interrogation.outcome,
                    interrogation.objection,
                    len(interrogation.turns),
                    interrogation.cost_usd,
                    now,
                ),
            )
            ask_id = int(cursor.lastrowid)

            for turn in interrogation.turns:
                answer_cursor = self.conn.execute(
                    "INSERT INTO answers (ask_id, turn, question, answer, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (ask_id, turn.turn, turn.question, turn.answer, now),
                )
                answer_id = int(answer_cursor.lastrowid)

                self.conn.executemany(
                    "INSERT INTO criterion_results (answer_id, criterion, met)"
                    " VALUES (?, ?, ?)",
                    [
                        (answer_id, result.criterion, int(result.met))
                        for result in turn.criteria
                    ],
                )

        return ask_id
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest tests/test_storage.py -v`
Expected: PASS — `TestNextProbe` and `TestRecordAsk` plus every pre-existing storage test.

- [ ] **Step 8: Run the whole offline suite**

Run: `.venv/bin/pytest`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/grill/storage.py tests/test_storage.py
git commit -m "feat: asks, answers, and criterion_results, plus newest-unasked probe selection"
```

---

### Task 6: `cli.py` — the entry point

The only module in this plan that touches a terminal.

**Files:**
- Create: `src/grill/cli.py`
- Modify: `pyproject.toml` — add `grill = "grill.cli:main"` to `[project.scripts]`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Store`, `Store.next_probe`, `Store.record_ask` from Task 5; `ask`, `Console` from Task 4.
- Produces:
  - `TerminalConsole` — the real `Console`
  - `main(argv: list[str] | None = None) -> int`

**Design notes.**

- **Ctrl-C aborts without recording.** `KeyboardInterrupt` propagates out of `input()`, `main` catches it, nothing is written, exit 130. The alternative — treating it as a skip — burns the probe permanently on a stray keypress, because `UNIQUE(probe_id)` means a consumed probe never returns. EOF (Ctrl-D) returns `""`, which the loop already reads as a deliberate skip.
- **No probe prints one line and exits 0.** Silence is right for a tool that pushes; a command you typed that prints nothing looks broken.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
"""Tests for the entry point.

`ask` is injected, so nothing here calls a model. What is worth pinning down is
the wiring and the two exits a developer hits by accident: an empty queue, and
Ctrl-C. The second one matters more than it looks — a probe is consumed by the
`asks` row that records it, so an interrupt that recorded a skip would destroy
the question on a stray keypress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grill.ask import Interrogation, PendingProbe
from grill.cli import main
from grill.judge import Rubric
from grill.storage import Store

RUBRIC = Rubric(
    topic="idempotency of the retry path",
    hypothesis="the developer accepted the key without knowing what it dedupes against",
    criteria=("Says the key must be stable across retries.",),
)

PENDING = PendingProbe(
    probe_id=7,
    question="What would break if the key were regenerated?",
    rubric=RUBRIC,
    created_at="2026-07-21T09:00:00+00:00",
)


class FakeStore:
    def __init__(self, pending: PendingProbe | None) -> None:
        self.pending = pending
        self.recorded: list[Interrogation] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def next_probe(self):
        return self.pending

    def record_ask(self, interrogation):
        self.recorded.append(interrogation)
        return 1


def an_interrogation() -> Interrogation:
    return Interrogation(
        probe_id=7,
        outcome="passed",
        confidence=95,
        objection=None,
        turns=(),
        cost_usd=0.02,
        reasoning="You named it.",
    )


def test_no_pending_probe_says_so_and_exits_zero(capsys):
    store = FakeStore(None)

    code = main([], store_factory=lambda: store, ask=lambda *a, **k: an_interrogation())

    assert code == 0
    assert capsys.readouterr().out.strip() != ""
    assert store.recorded == []


def test_records_the_interrogation(capsys):
    store = FakeStore(PENDING)
    seen = {}

    def ask(pending, console, **kwargs):
        seen["pending"] = pending
        return an_interrogation()

    code = main([], store_factory=lambda: store, ask=ask)

    assert code == 0
    assert seen["pending"] is PENDING
    assert len(store.recorded) == 1
    assert store.recorded[0].outcome == "passed"


def test_an_interrupt_records_nothing(capsys):
    """A stray Ctrl-C must not consume the probe.

    UNIQUE(probe_id) means an `asks` row is permanent, so recording an
    interrupt as a skip would destroy the question rather than defer it.
    """
    store = FakeStore(PENDING)

    def ask(pending, console, **kwargs):
        raise KeyboardInterrupt

    code = main([], store_factory=lambda: store, ask=ask)

    assert code == 130
    assert store.recorded == []


def test_the_real_store_sees_the_new_tables(tmp_path: Path):
    """The schema is additive and applied on open, so there is no migration step."""
    with Store(tmp_path / "grill.db") as store:
        names = {
            row["name"]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"asks", "answers", "criterion_results"} <= names
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grill.cli'`

- [ ] **Step 3: Write `cli.py`**

Create `src/grill/cli.py`:

```python
"""`grill` — ask me the question.

The only module here that owns a terminal. Everything it knows about interaction
lives in `TerminalConsole`; everything it knows about interrogation it delegates
to `ask.py`, which has never heard of a TTY. That split is what keeps the
delivery question open: a hook, a nudge, or a prompt injection would replace this
file and nothing else.

Deliberately not registered in settings.json. This is invoked by hand.
"""

from __future__ import annotations

import argparse

from grill.ask import Console, ask as _ask
from grill.storage import Store

NOTHING_PENDING = "nothing to ask about."


class TerminalConsole:
    """The real console: print and `input`.

    `EOFError` becomes an empty string, which the loop already reads as a skip —
    Ctrl-D is a deliberate "not now". `KeyboardInterrupt` is deliberately NOT
    caught here; `main` handles it by recording nothing at all.
    """

    def show(self, text: str) -> None:
        print(text)

    def prompt(self, text: str) -> str:
        try:
            return input(f"{text}\n> ")
        except EOFError:
            return ""


def main(
    argv: list[str] | None = None,
    *,
    store_factory=Store,
    ask=_ask,
    console: Console | None = None,
) -> int:
    """Take one pending probe, interrogate, record. Returns a shell exit code."""
    parser = argparse.ArgumentParser(
        prog="grill", description="Answer one question about something you shipped."
    )
    parser.parse_args(argv)

    with store_factory() as store:
        pending = store.next_probe()
        if pending is None:
            # A command you typed that prints nothing looks broken. Silence is
            # for the tools that push; this one was asked for.
            print(NOTHING_PENDING)
            return 0

        try:
            interrogation = ask(pending, console or TerminalConsole())
        except KeyboardInterrupt:
            # Record nothing. An `asks` row is permanent (UNIQUE on probe_id), so
            # writing one here would consume the probe on a stray keypress rather
            # than leaving it for the next run.
            print()
            return 130

        store.record_ask(interrogation)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Register the script**

In `pyproject.toml`, change `[project.scripts]` to:

```toml
[project.scripts]
grill = "grill.cli:main"
grill-hook = "grill.hook:main"
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Reinstall and check the entry point exists**

```bash
.venv/bin/pip install -e . --no-deps -q
.venv/bin/grill --help
```

Expected: argparse usage text naming `grill`. This does not touch the database.

- [ ] **Step 7: Run `grill` against the live database**

```bash
.venv/bin/grill
```

Expected: `nothing to ask about.` — the live db holds two sessions, both `silent`, so there are no probes. If it prints a question instead, something has produced a probe since this plan was written; answer it or Ctrl-C out (which records nothing).

- [ ] **Step 8: Run the whole offline suite**

Run: `.venv/bin/pytest`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/grill/cli.py tests/test_cli.py pyproject.toml
git commit -m "feat: the grill command — one probe, interrogated, recorded"
```

---

### Task 7: One real interrogation, measured

Everything above is measured against mocks except Task 2. This is the first time the loop runs against a real judge writing real follow-ups, and the spec asks for a measured number rather than an estimate.

**Files:**
- Create: `tests/test_ask_smoke.py`
- Create: `docs/measurements/2026-07-22-interrogation-cost.md` (date it the day you run it)

**Interfaces:**
- Consumes: everything from Tasks 1, 3, 4, 5.
- Produces: nothing code depends on. A cost figure and an observation.

**Fixture choice.** Use a probe from `docs/measurements/2026-07-21-generated-rubric-calibration/generated_probes.json` rather than whatever happens to be in the live database. Those probes are committed, so this measurement is reproducible, and their rubrics came out of the real pipeline. Follow the precedent in `tests/test_judge_calibration.py`, whose docstring is explicit that a hand-typed topic exercises the one path where the fatal failure cannot occur.

- [ ] **Step 1: Write the calibration test**

Create `tests/test_ask_smoke.py`:

```python
"""One real interrogation, end to end, against a real judge and real follow-ups.

Marked `calibration`: it calls the model and costs money. Everything else about
`ask.py` is tested against a scripted console at zero spend; what this adds is
the two things a mock cannot show — what a full interrogation actually costs, and
whether a generated follow-up reads like a question a person would answer.

The probe is a committed artifact rather than whatever is in the live database,
so this is reproducible and its rubric came from the real pipeline.

Run it deliberately:

    .venv/bin/pytest -m calibration tests/test_ask_smoke.py -s
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grill.ask import PASSED, FAILED, PendingProbe, ask
from grill.judge import Rubric

pytestmark = pytest.mark.calibration

PROBES = Path("docs/measurements/2026-07-21-generated-rubric-calibration/generated_probes.json")
SESSION = "0aea1b38-e347-4a3c-b689-d2f14fc9736c"

# Deliberately partial: correct that status alone cannot distinguish a resumed
# run, silent on what the guard actually buys. That is the shape the loop exists
# for — enough to earn a follow-up, not enough to pass on turn one.
PARTIAL = (
    "It'd still fail it. The guard only looks at status, and a run that started "
    "heartbeating again is still 'running', so the update matches and flips it to "
    "failed anyway."
)

# The piece the partial answer left out.
COMPLETION = (
    "What the guard actually buys is idempotency against a double terminal "
    "write — the second writer matches zero rows. To close the real race you'd "
    "have to re-check heartbeat_at inside the UPDATE's WHERE, not just status."
)


class RecordingConsole:
    """Replays two answers and prints everything, so `-s` shows the interrogation."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.shown: list[str] = []

    def show(self, text: str) -> None:
        self.shown.append(text)
        print(f"  | {text}")

    def prompt(self, text: str) -> str:
        reply = self.answers.pop(0) if self.answers else ""
        print(f"  ? {text}\n  > {reply}")
        return reply


def test_one_real_interrogation():
    entry = json.loads(PROBES.read_text())[SESSION]
    pending = PendingProbe(
        probe_id=0,
        question=entry["probe"]["question"],
        rubric=Rubric(
            topic=entry["seed"]["topic"],
            hypothesis=entry["seed"]["hypothesis"],
            criteria=tuple(entry["probe"]["criteria"]),
        ),
        created_at="2026-07-21T09:00:00+00:00",
    )
    console = RecordingConsole(["70", PARTIAL, COMPLETION])

    result = ask(pending, console)

    print(f"\n  outcome: {result.outcome}")
    print(f"  turns:   {len(result.turns)}")
    print(f"  cost:    ${result.cost_usd:.4f}")
    for turn in result.turns:
        print(f"  [{turn.turn}] {turn.question}")
        for criterion in turn.criteria:
            print(f"      {'MET ' if criterion.met else 'MISS'} {criterion.criterion}")

    # Not asserting PASSED. Whether this specific answer clears this specific
    # rubric is the judge's call, and pinning it here would turn a measurement
    # into a test of the model's mood. What must hold is that the loop reached a
    # real verdict rather than an error or a skip.
    assert result.outcome in (PASSED, FAILED)
    assert result.cost_usd and result.cost_usd > 0
    assert len(result.turns) >= 1
```

- [ ] **Step 2: Confirm it is deselected by default**

Run: `.venv/bin/pytest --collect-only -q | tail -3`
Expected: the deselected count rises by one (from 4 to 5). If `test_one_real_interrogation` appears as collected, the `pytestmark` is missing and the next `pytest` run will spend money.

- [ ] **Step 3: Run it**

```bash
.venv/bin/pytest -m calibration tests/test_ask_smoke.py -s
```

Expected: PASS, with the interrogation printed. Read the follow-up question it generated before moving on.

- [ ] **Step 4: Write the measurement up**

Create `docs/measurements/2026-07-22-interrogation-cost.md`:

- Measured cost of one interrogation, from the test's own output — judge calls plus follow-up calls, broken out.
- Turns taken, and the outcome.
- **The generated follow-up, quoted verbatim, with a judgement**: did it aim at the unmet criterion, or restate the original question in different words? Did it leak its own answer? This is the part no unit test can check and the part most likely to be wrong — follow-ups are generated, and `probe.py` already needed a structural gate to keep its first question honest.
- The worst-case cost: `len(criteria)` judge calls plus `len(criteria) - 1` follow-ups, at 4 criteria. Extrapolate from the measured per-call figures and say it is an extrapolation.
- Compare against the spec's rough estimate and say whether it held.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ask_smoke.py docs/measurements/
git commit -m "test: one real interrogation, measured end to end"
```

---

## After the plan

Do not merge to `main` until:

1. **Task 2 reproduced 45/45**, and the breakdown check in its Step 3 passed. This is the merge condition the spec names.
2. `.venv/bin/pytest` is green — 125 existing tests plus roughly 60 new ones.
3. `.venv/bin/grill` runs against the live database without a traceback.

Then use `superpowers:finishing-a-development-branch`.

## Known-open, carried forward from the spec

- **Stage 1 topic instability.** `context_line()` renders `seeds.topic`, so an arbitrarily-named topic is now the first thing a human reads. This plan does not fix it, and shipping the CLI makes it visible rather than merely present in a database. It is the strongest argument yet for fixing it.
- **Discovery is undecided.** `ask.py` takes a `Console` and knows nothing about its caller, so a SessionStart hook or a nudge replaces `cli.py` alone.
- **The pass/fail boundary is still unmeasured** for the per-criterion judge, exactly as it was for the boolean one. `run_boundary.py` exists and would need rerunning to say anything about partial answers under the new grading. Task 2 does not cover it, and a clean 45/45 must not be read as if it did.
- **Consecutive skips are derivable but unused.** The three-skip mute belongs with a delivery surface that pushes; the data it needs is recorded from day one.
