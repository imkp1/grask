# Stage 1: Split Extraction from Selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make stage 1's topic selection reproducible by having the LLM enumerate every qualifying moment and a deterministic ranker pick the winner.

**Architecture:** Today one LLM call both judges *whether* a session qualifies and picks *which* moment to ask about. Measurement shows the judging is stable and the picking is not: sessions carry 2–9 qualifying moments (median ~6) and the call picks among them arbitrarily. This splits the seam — `triage.py` enumerates and validates moments, a new `select.py` ranks them with pure code, and `triage()` composes the two. The public `TriageVerdict` shape is preserved so `triage_run.py` and stage 2 see no change beyond a new `moments` field.

**Tech Stack:** Python 3.12, `uv`, pytest. No new dependencies. LLM access stays behind `grill.llm.complete`.

## Measurement this plan rests on

Run on 2026-07-20 over the 6 richest sessions in the local corpus, 3 runs each (18 calls, $0.90). Recorded here because the plan's design choices are downstream of it and nothing in the repo captures it:

- Candidate turn sets identical across all 3 runs: **3/6 sessions**. The 3 that differed never *disagreed* about a moment — each differing run added or dropped 1–2 marginal moments while the core set held.
- Signal label for a given turn identical across all 3 runs: **26/29 moments**.
- Topic wording for a given turn: stable in meaning across runs.
- Qualifying moments per session: 2, 3, 5, 7, 9, 9.

**Conclusion:** the dominant instability is arbitrary selection among many valid candidates, not unstable extraction. Deterministic ranking addresses the dominant cause. See "Known residual instability" — it does not eliminate all of it, and this plan does not claim it does.

## Global Constraints

- No `--model` flag anywhere. Every call inherits the user's Claude Code selection (design, "Model selection").
- `grill.llm` remains the only module that knows a subprocess exists.
- Stage 1 sees developer turns and file *paths* only, never file contents.
- `triage()` never raises. A stage that can crash the hook is a stage that speaks on failure.
- The evidence rule is enforced in code, not prompted for. A quote that cannot be verified demotes rather than passes downstream.
- Tests call no LLM.

## Decision: ranking order diverges from the design doc

The design doc (`specs/2026-07-17-grill-design.md:329-336`) ranks topics:

1. asked_why 2. explained_at_length 3. new_pattern 4. pushed_back

**This plan uses a different order:** `asked_why` > `pushed_back` > `new_pattern` > `explained_at_length`.

Reason: `explained_at_length` and `new_pattern` are the two signals a quote cannot prove — `triage.py:38-40` already classifies them `CODE_GROUNDED` and marks keeps on them `weak_evidence`, pending stage 2 grounding them in the diff. Ranking them *above* `pushed_back` would make selection systematically prefer the moments with the weakest evidence, undoing commit `5d064c3`. `pushed_back` is also the most common signal in the corpus, so demoting it to last would leave many sessions selecting on weak evidence alone.

Task 6 updates the design doc to match. **If the design's order is deliberate and should win, change `SIGNAL_RANK` in Task 2 and say so — do not silently keep both.**

## Known residual instability

Deterministic ranking does not make selection fully reproducible. When a marginal moment — one extraction finds on some runs only — lands in the top-ranked signal class, it can win on the runs where it appears. In the measured sample this affects 1 of 6 sessions (`46c50844`, where a wobbly `asked_why` at turn 18 competes with a stable one at turn 12), and no tiebreak rule fixes it, because the disagreement is upstream in extraction.

This is accepted, not solved. It is a large improvement over choosing arbitrarily among 9 candidates, and unlike today it is *detectable* — re-running extraction on one session exposes it. Do not add a second extraction pass and intersect the results to chase it; that doubles per-session cost for a case measured at ~17%, and the cost of a stage-1 call is the design's binding constraint. Revisit only if the observed rate rises.

## File Structure

- `src/grill/llm.py` — **modify.** Add `extract_json_array`, the array counterpart of the existing `extract_json_object`. Belongs here because response-shape recovery already lives here.
- `src/grill/triage.py` — **modify.** Prompt becomes "enumerate every moment"; add `Moment`; `parse_verdict` becomes `parse_moments` enforcing the evidence rule per moment; `triage()` composes extraction and selection.
- `src/grill/select.py` — **create.** Pure, LLM-free, deterministic ranking. Separate file because it is the one part of stage 1 that is fully testable without a model, and keeping it independent is the point of the change.
- `src/grill/triage_run.py` — **modify.** Report candidate counts so future instability is visible in the corpus run.
- `tests/test_triage.py` — **modify.** Evidence-rule tests move to per-moment.
- `tests/test_select.py` — **create.** Ranking and tie-breaking.

---

### Task 1: Array extraction in the LLM adapter

**Files:**
- Modify: `src/grill/llm.py` (add after `extract_json_object`, ends line 205)
- Test: `tests/test_triage.py`

**Interfaces:**
- Produces: `extract_json_array(text: str) -> list[dict]` — returns the first JSON array of objects in a model response; raises `LLMError` if there is none. Non-dict elements are discarded.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_triage.py`, importing `extract_json_array` from `grill.llm` alongside the existing imports:

```python
class TestExtractJsonArray:
    def test_parses_a_bare_array(self):
        assert extract_json_array('[{"turn": 3}]') == [{"turn": 3}]

    def test_parses_a_fenced_array_with_preamble(self):
        text = 'Here are the moments:\n```json\n[{"turn": 3}, {"turn": 7}]\n```'
        assert extract_json_array(text) == [{"turn": 3}, {"turn": 7}]

    def test_parses_an_empty_array(self):
        assert extract_json_array("[]") == []

    def test_survives_a_bracket_inside_a_string_value(self):
        # Developer turns are rendered as "[7] text", so quotes routinely
        # contain brackets. Naive bracket matching truncates the array here.
        text = '[{"quote": "why does [7] matter?"}]'
        assert extract_json_array(text) == [{"quote": "why does [7] matter?"}]

    def test_discards_non_object_elements(self):
        assert extract_json_array('[{"turn": 1}, "junk", 5]') == [{"turn": 1}]

    def test_raises_when_there_is_no_array(self):
        with pytest.raises(LLMError):
            extract_json_array("I could not find anything worth asking about.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_triage.py::TestExtractJsonArray -v`
Expected: FAIL — `ImportError: cannot import name 'extract_json_array' from 'grill.llm'`

- [ ] **Step 3: Implement**

Append to `src/grill/llm.py`:

```python
def extract_json_array(text: str) -> list[dict]:
    """Pull the first JSON array of objects out of a model response.

    The array counterpart of `extract_json_object`. Brace-matching rather than a
    regex, and string-aware: rendered turns are prefixed `[n]`, so quotes very
    often contain brackets that naive matching would treat as structure.

    No salvage pass. `salvage_flat_object` recovers one flat object by reading to
    the last quote before the next known key; an array has no such anchor, and a
    half-recovered list of moments is worse than none — it would silently drop
    moments and make the enumeration look unstable when the model was fine.
    """
    text = text.strip()
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            char = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, list):
                        return [item for item in parsed if isinstance(item, dict)]
                    break
        start = text.find("[", start + 1)

    raise LLMError(f"no JSON array in response: {text[:400]}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_triage.py::TestExtractJsonArray -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/grill/llm.py tests/test_triage.py
git commit -m "feat: extract JSON arrays from model responses"
```

---

### Task 2: Deterministic selection

**Files:**
- Create: `src/grill/select.py`
- Test: `tests/test_select.py`

**Interfaces:**
- Consumes: nothing. Deliberately independent of `triage.py` — it ranks anything with `.signal` and `.turn`, which is what keeps it testable without a model. `Moment` arrives in Task 3; these tests use a local stub with the same two attributes.
- Produces: `SIGNAL_RANK: dict[str, int]`, `rank_key(moment) -> tuple[int, int]`, `select(moments: Sequence) -> object | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_select.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_select.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grill.select'`

- [ ] **Step 3: Implement**

Create `src/grill/select.py`:

```python
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

from typing import Protocol, Sequence

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


def select(moments: Sequence[Rankable]) -> Rankable | None:
    """The one moment worth a question, or None if there were none."""
    if not moments:
        return None
    return min(moments, key=rank_key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_select.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/grill/select.py tests/test_select.py
git commit -m "feat: deterministic moment selection"
```

---

### Task 3: Enumerate moments instead of picking one

**Files:**
- Modify: `src/grill/triage.py:42-119` (prompt), `:26-28` (keys), `:122-145` (add `Moment`), `:189-244` (replace `parse_verdict`), `:247-268` (`triage`)
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `extract_json_array` (Task 1), `select` (Task 2)
- Produces:
  - `Moment` dataclass: `turn: int`, `signal: str`, `topic: str`, `quote: str`, `shows: str`, `weak_evidence: bool = False`
  - `parse_moments(session: Session, completion: Completion) -> tuple[list[Moment], list[str]]` — validated moments, plus a rejection reason per moment dropped by the evidence rule
  - `TriageVerdict` gains `moments: list[Moment]` and `candidates: int`; every existing field keeps its meaning

- [ ] **Step 1: Write the failing tests**

Replace the `TestParseVerdict` class in `tests/test_triage.py` with the following. In the imports at the top, replace `VERDICT_KEYS` and `parse_verdict` with `Moment` and `parse_moments`:

```python
def moments_json(*moments: dict) -> str:
    import json

    return json.dumps(list(moments))


def moment(**overrides) -> dict:
    base = {
        "turn": 0,
        "signal": "asked_why",
        "topic": "idempotency keys",
        "quote": "why an idempotency key?",
        "shows": "the developer asking why this key is needed",
    }
    base.update(overrides)
    return base


class TestParseMoments:
    def test_keeps_a_moment_whose_quote_is_in_the_named_turn(self):
        found, rejected = parse_moments(
            session("why an idempotency key?"), completion(moments_json(moment()))
        )
        assert [m.topic for m in found] == ["idempotency keys"]
        assert found[0].turn == 0
        assert rejected == []

    def test_keeps_every_qualifying_moment(self):
        text = moments_json(
            moment(turn=0),
            moment(turn=1, signal="pushed_back", quote="no, use a ledger", topic="ledgers"),
        )
        found, _ = parse_moments(
            session("why an idempotency key?", "no, use a ledger"), completion(text)
        )
        assert len(found) == 2

    def test_rejects_a_fabricated_quote(self):
        found, rejected = parse_moments(
            session("ship it"), completion(moments_json(moment()))
        )
        assert found == []
        assert "not found" in rejected[0]

    def test_rejects_a_quote_that_is_in_a_different_turn(self):
        # Anchoring to the named turn is stricter than the old whole-session
        # search, and it is what makes the turn index trustworthy as identity.
        found, rejected = parse_moments(
            session("ship it", "why an idempotency key?"),
            completion(moments_json(moment(turn=0))),
        )
        assert found == []
        assert "not found" in rejected[0]

    def test_rejects_a_turn_index_the_session_does_not_have(self):
        found, rejected = parse_moments(
            session("why an idempotency key?"), completion(moments_json(moment(turn=42)))
        )
        assert found == []
        assert "no turn 42" in rejected[0]

    def test_rejects_asked_why_when_the_quote_asks_nothing(self):
        found, rejected = parse_moments(
            session("we should use a ledger"),
            completion(moments_json(moment(quote="we should use a ledger"))),
        )
        assert found == []
        assert "asks nothing" in rejected[0]

    def test_rejects_an_unrecognized_signal(self):
        found, rejected = parse_moments(
            session("why an idempotency key?"),
            completion(moments_json(moment(signal="vibes"))),
        )
        assert found == []
        assert "unrecognized signal" in rejected[0]

    def test_marks_code_grounded_signals_weak(self):
        found, _ = parse_moments(
            session("why an idempotency key?"),
            completion(moments_json(moment(signal="new_pattern"))),
        )
        assert found[0].weak_evidence is True

    def test_quote_provable_signals_are_not_weak(self):
        found, _ = parse_moments(
            session("why an idempotency key?"), completion(moments_json(moment()))
        )
        assert found[0].weak_evidence is False

    def test_one_bad_moment_does_not_discard_the_good_ones(self):
        text = moments_json(moment(turn=0), moment(turn=1, quote="never typed this"))
        found, rejected = parse_moments(
            session("why an idempotency key?", "ok"), completion(text)
        )
        assert len(found) == 1
        assert len(rejected) == 1

    def test_an_empty_array_is_silence_not_an_error(self):
        found, rejected = parse_moments(session("ship it"), completion("[]"))
        assert found == []
        assert rejected == []


class TestTriageVerdictFromMoments:
    def test_selected_moment_populates_the_verdict(self, monkeypatch):
        text = moments_json(
            moment(turn=0, signal="pushed_back", quote="no, use a ledger", topic="ledgers"),
            moment(turn=1, quote="why an idempotency key?"),
        )
        monkeypatch.setattr(
            "grill.triage.complete", lambda prompt, **kw: completion(text)
        )
        verdict = triage(session("no, use a ledger", "why an idempotency key?"))
        assert verdict.kept
        # asked_why outranks pushed_back regardless of position in the response.
        assert verdict.signal == "asked_why"
        assert verdict.topic == "idempotency keys"
        assert verdict.candidates == 2
        assert len(verdict.moments) == 2

    def test_no_qualifying_moments_is_silence(self, monkeypatch):
        monkeypatch.setattr("grill.triage.complete", lambda prompt, **kw: completion("[]"))
        verdict = triage(session("ship it"))
        assert not verdict.kept
        assert verdict.candidates == 0

    def test_all_moments_rejected_records_the_demotion(self, monkeypatch):
        text = moments_json(moment(quote="never typed this"))
        monkeypatch.setattr("grill.triage.complete", lambda prompt, **kw: completion(text))
        verdict = triage(session("ship it"))
        assert not verdict.kept
        assert verdict.demoted_from_ask is True
        assert "not found" in verdict.reason

    def test_an_llm_failure_is_silence_not_a_crash(self, monkeypatch):
        def boom(prompt, **kw):
            raise LLMError("claude timed out after 180s")

        monkeypatch.setattr("grill.triage.complete", boom)
        verdict = triage(session("why an idempotency key?"))
        assert not verdict.kept
        assert verdict.error == "claude timed out after 180s"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_triage.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_moments' from 'grill.triage'`

- [ ] **Step 3: Replace the prompt**

In `src/grill/triage.py`, delete `VERDICT_KEYS` and its comment (lines 26-28). It existed to drive `salvage_flat_object`, which the array parser deliberately has no counterpart to — see the docstring in Task 1.

Then replace the `PROMPT` string (lines 42-119) with:

```python
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
```

- [ ] **Step 4: Add the Moment dataclass and extend TriageVerdict**

In `src/grill/triage.py`, insert before `class TriageVerdict`:

```python
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
```

Then in `TriageVerdict`, add two fields after `weak_evidence` (line 141):

```python
    # Every qualifying moment, not just the selected one. Stage 2 gets the
    # runner-up for free, and dedup gets something to work with when the top
    # moment has already been asked about.
    moments: list[Moment] = field(default_factory=list)
    candidates: int = 0
```

Update the import at line 15 to `from dataclasses import dataclass, field`, and the import at line 17 to:

```python
from grill.llm import Completion, LLMError, complete, extract_json_array
from grill.select import select
```

- [ ] **Step 5: Replace parse_verdict with parse_moments**

Replace `parse_verdict` (lines 189-244) with:

```python
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

    for raw in extract_json_array(completion.text):
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

        label = f"turn {turn_index}"
        if not isinstance(turn_index, int) or turn_index not in by_index:
            rejected.append(f"{label}: no turn {turn_index} in this session")
            continue
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
```

- [ ] **Step 6: Rewrite triage() to compose extraction and selection**

Replace `triage` (lines 247-268) with:

```python
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
        moments, rejected = parse_moments(session, completion)
    except LLMError as exc:
        return TriageVerdict(
            session_id=session.session_id,
            verdict="silent",
            reason="triage failed",
            error=str(exc),
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
```

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest tests/ -v`
Expected: PASS. Any remaining failure is a stale reference to `parse_verdict` or `VERDICT_KEYS` in `tests/test_triage.py` — delete those tests; the behaviour they covered is now covered per-moment.

- [ ] **Step 8: Commit**

```bash
git add src/grill/triage.py tests/test_triage.py
git commit -m "feat: enumerate moments, select deterministically"
```

---

### Task 4: Surface candidate counts in the corpus runner

**Files:**
- Modify: `src/grill/triage_run.py:26-42` (`_row`), `:52-97` (`report`)

**Interfaces:**
- Consumes: `TriageVerdict.moments`, `TriageVerdict.candidates` (Task 3)
- Produces: no new API. `triage-results.json` rows gain `candidates` and `moments`.

- [ ] **Step 1: Add the fields to each row**

In `_row`, after the `"weak_evidence"` entry, add:

```python
        "candidates": verdict.candidates,
        "moments": [
            {
                "turn": m.turn,
                "signal": m.signal,
                "topic": m.topic,
                "quote": m.quote,
                "weak_evidence": m.weak_evidence,
            }
            for m in verdict.moments
        ],
```

- [ ] **Step 2: Report the distribution**

In `report`, after the `cost` line in the summary block, add:

```python
        f"  candidates     {sum(r['candidates'] for r in rows):>4d} moments across all sessions"
        f"   (max {max((r['candidates'] for r in rows), default=0)} in one)",
```

And in the per-session KEPT loop, after the `shows:` line, add:

```python
        if r["candidates"] > 1:
            others = [m for m in r["moments"] if m["topic"] != r["topic"]]
            lines.append(
                "            also: " + _clip("; ".join(m["topic"] for m in others), 62)
            )
```

- [ ] **Step 3: Verify it runs without spending much**

Run: `uv run python -m grill.triage_run --limit 3 --out /tmp/grill-smoke.json`
Expected: a report naming a `candidates` total, and for any kept session with more than one moment, an `also:` line listing the runners-up. Cost roughly $0.08/session.

- [ ] **Step 4: Commit**

```bash
git add src/grill/triage_run.py
git commit -m "feat: report candidate moments per session"
```

---

### Task 5: Re-run the corpus and record the result

This task spends money (~64 sessions x ~$0.078 ≈ $5) and requires human judgement. Do not skip it: the 15/49 split in the README predates both the signal check and this change, and `README.md:44` already admits the re-run is owed.

**Files:**
- Modify: `README.md:37-44`

- [ ] **Step 1: Run the full corpus**

Run: `uv run python -m grill.triage_run --out triage-results.json`
Expected: a kept/dropped split, a candidate total, and no `[ERROR]` rows.

- [ ] **Step 2: Read every keep and every demotion**

The output that matters is not that it executed. For each kept session, check the quote supports the signal, and check the selected topic is the best of the listed candidates — if a runner-up on the `also:` line is plainly better, the ranking is wrong and Task 2's `SIGNAL_RANK` is the thing to revisit. A high demotion rate is a bug report against the prompt, not against the developer.

- [ ] **Step 3: Update the README with the measured numbers**

Replace `README.md:37-44` with the real figures, and delete the "Re-running the corpus is pending" sentence. State the kept/dropped split, the candidate count distribution, and that selection is now deterministic given the moment list.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: re-measure stage 1 after the extraction/selection split"
```

---

### Task 6: Record the design decisions in the spec

**Files:**
- Modify: `docs/superpowers/specs/2026-07-17-grill-design.md:329-336` and the cost section

- [ ] **Step 1: Update "What counts as a topic"**

Reorder the ranked list to match `select.SIGNAL_RANK` — asked_why, pushed_back, new_pattern, explained_at_length — and add a sentence recording why: the last two cannot be proven from a quote, so preferring them would make selection favour the weakest evidence. Note that ranking is enforced in `select.py`, not prompted for.

- [ ] **Step 2: Record the extraction/selection split**

Add a short subsection under stage 1 stating that the LLM enumerates moments and code selects among them, with the measurement that motivated it: 6 sessions x 3 runs, identical keep decisions, 2-9 candidates per session, arbitrary selection among them being the observed instability. Record the residual case from "Known residual instability" so a future reader does not read the split as a complete fix.

- [ ] **Step 3: Fix the cost section**

The cost section still does not mention the per-call floor. Add it: grill's own prompt is ~400 tokens and everything else is inherited Claude Code context, which scales with the user's config rather than with grill — measured at 20.5k -> 7.8k tokens cold with `--disable-slash-commands` on a 1822-skill config, and ~$0.011 warm. Note that `--bare` is rejected because it requires `ANTHROPIC_API_KEY`.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-17-grill-design.md
git commit -m "docs: record ranking order and the extraction/selection split"
```

---

## Deliberately not in this plan

- **Dedup across sessions.** Needs the SQLite state that does not exist. Task 3 gives it what it was missing — a ranked list, so a session whose top moment was already asked about can fall through to the runner-up instead of going silent.
- **The paste-contamination hole.** The evidence rule checks a quote is verbatim, not that the developer authored the thought; one keep in the last corpus run quoted them pasting their own previous code back in. Task 3's per-turn anchoring is the precondition for fixing it — the check needs the containing turn, which `parse_moments` now has — but the fix itself is a separate change with its own measurement.
- **A second extraction pass to intersect candidate sets.** See "Known residual instability".
