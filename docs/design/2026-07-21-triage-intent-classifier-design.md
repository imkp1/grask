# Stage 1 intent classifier — Design

**Date:** 2026-07-21
**Status:** ⏸ **Paused — do not implement.** See "Why this is paused".
**Scope:** stage 1 triage; breaking contract change to stage 2
**Supersedes:** the four-signal taxonomy in `2026-07-17-grill-design.md` ("What counts as a topic")

## Why this is paused

This design refines the ranking of a stage whose output has never been consumed by a
question. Stage 2 does not exist. Nothing downstream has ever read an `intent`.

Its own Gate 2 asks for a $3.50 corpus run plus session-by-session hand-judgement to
validate a taxonomy — judging moment selection in isolation, against the author's taste,
with no evidence about whether any of those moments yields a question worth answering. That
is an expensive instrument pointed at the wrong question, and it would be the second time
stage 1 was tuned this way.

The build order in `2026-07-17-grill-design.md` always put the core first. The repo drifted;
this spec is that drift continuing. The correct next build is the loop — hypothesis →
rubric → probe → answer → judge → **was this worth asking?** — fed by the 28 keeps this
corpus run already produced.

**What survives when this resumes:**

- The measurement is durable. `new_pattern` and `explained_at_length` are structurally
  unreachable regardless of anything downstream; deleting them stands.
- The taxonomy is now subordinate to a stated invariant in the parent design — *what
  evidence suggests the developer accepted something without fully understanding it?* Under
  it, `contradiction` and `pushed_back` become drop candidates rather than low-ranked
  intents.
- `concern` is superseded by **Hypothesis** — a falsifiable claim, not a noun phrase. The
  merge should group by hypothesis. Whether hypotheses drift less than noun-phrase labels is
  open, and is Gate 1's real question.
- Gate 2 should be re-specified against yes-rate rather than hand-judgement.

## Premise

Stage 1 answers two questions: does this session contain anything worth asking about,
and which moment is it. A previous change split those apart — the LLM enumerates
moments, code selects among them — which made selection reproducible. It did not make
selection *good*, and it left the taxonomy those moments are classified into untouched.

A 69-session corpus run and a session-by-session human review of the result showed the
taxonomy is the weaker half.

## What the measurement showed

Corpus run: 69 sessions, 28 kept (41%), $3.53, zero errors. Of the 28 keeps, 15 had
more than one candidate moment and were judged by hand.

| Finding | Evidence |
|---|---|
| Half the taxonomy never fires | `new_pattern` 0 candidates in 69 sessions; `explained_at_length` 1 |
| The one `explained_at_length` hit was malformed | quote was the developer *asking* for an explanation, not receiving one |
| `asked_why` absorbs everything | wins 20 of 28 keeps despite being 30 of 62 candidates |
| Ranking held up | `SIGNAL_RANK` correct on 7 of 8 judged cross-signal decisions |
| The tiebreak is weak | recency correct on 4 of 7 judged same-signal ties |
| Ties are common | 10 of 28 keeps decided by tiebreak rather than rank |

Judged sessions: 6 correct, 4 wrong, 5 unrecalled. Per-session judgements in
`docs/measurements/2026-07-20-corpus-run-judgements.md`; raw report in
`docs/measurements/2026-07-20-corpus-run.txt`.

### Why two signals never fire

`new_pattern` and `explained_at_length` are defined as code-grounded — "did something
land in this developer's codebase whose rationale they may have accepted on faith?"
Stage 1 sees only developer turns. The prompt states this itself: "Only the developer's
own words appear below; there is nothing else to quote." Combined with the evidence rule
("no quote, no moment"), those two signals ask the model to evidence something it cannot
observe.

This is structural, not a tuning problem. No prompt change fixes it without giving stage 1
the assistant turns and the diff.

## Design

### Intent taxonomy

Six intents replace four signals. Ranked, lowest number wins.

| Rank | Intent | Tell | Why here |
|---|---|---|---|
| 0 | `definition_gap` | "what is X", "what do you mean by X" | Clearest gap; the explanation they received is wholly unverified |
| 1 | `asks_rationale` | plain "why X" | They took a rationale on faith |
| 2 | `counter_proposal` | "why not Y", "it should be Z" | They had an alternative and dropped it — did they learn why? |
| 3 | `asserts_belief` | "…right ?", "are we sure ?" | Belief may have been confirmed or corrected; stage 1 cannot tell which |
| 4 | `contradiction` | cites evidence against the agent | They were right and proved it — least left to teach |
| 5 | `pushed_back` | correction or override | The agent complied; nothing was explained to them |

The ordering principle is **quiz what they were told, not what they told the agent.** A
question they asked means a gap they had, an answer they received, and nobody ever
checked whether it stuck. An assertion is something they already believed walking in.

This inverts an earlier draft that ranked `asserts_belief` first on the grounds that a
confident wrong belief is the most dangerous. That draft was wrong for a structural
reason: stage 1 sees only developer turns, so it cannot know whether an assertion was
confirmed or corrected. Ranking it first assumes knowledge the stage does not have — the
same blindness that makes `new_pattern` unreachable.

The taxonomy was derived from the 30 `asked_why` quotes in the corpus run, not invented.
Cluster sizes there: ~7 `asserts_belief`, ~8 `asks_rationale`, ~5 `definition_gap`,
~4 `counter_proposal`, ~4 `contradiction`, ~2 consequence-probe. The consequence-probe
cluster is folded into `asks_rationale`; two examples is not enough to earn a rank.

### Guard: `definition_gap` names the moment, not the question

IDEA.md rejects exactly the question this rank invites: *"It isn't 'what is a retry
policy.' You can answer that one from memory, and answering it proves nothing."* That is
the product's stated unrecoverable failure.

Ranking `definition_gap` first is therefore a claim about which *moment* is worth
returning to, never about what to ask. Stage 2 must ground the question in the code the
user shipped — "your ranker does X, what happens when Y" — and must not ask for the
definition back. The trigger is that they did not know the term; the test is whether the
thing built on that term makes sense to them now.

This guard is load-bearing. Without it, rank 0 routes the highest-priority moments into
the lowest-value question form, and the failure would look like the taxonomy working.

### Concern merge

Sessions with several moments usually circle one concern rather than raising several.
`3ffb1ee5` is three angles on volume tenancy; `74c6c4e8` is two on whether a claim was
verified. These are not competing moments and no tiebreak can choose well among them.

Each moment gains a `concern` field: a short canonical label naming the underlying thing,
distinct from `topic` (which describes that specific turn). Code groups by normalized
concern — casefold, collapse whitespace — and collapses each group into one moment
carrying `turns: [0,3,6]` and `returns: 3`.

The representative `quote` / `topic` / `shows` come from the group member with the best
intent rank; ties within a group break toward the earliest turn.

Division of labour matches the existing extraction/selection split: the LLM does the
semantic judgement (which concern is this?), code does the grouping (deterministic string
match). Putting the merge in the prompt would move run-to-run variation into the
non-deterministic step, which is the problem the previous change existed to solve.

### Selection

```
rank_key = (best_intent_rank, -returns, first_turn)
```

Intent dominates recurrence. Recurrence-first was rejected: repetition more often means
the *agent* explained badly than that the developer's concern ran deep, so a developer
who asked "why is this needed?" four times out of frustration would outrank a genuine
definition gap. A bounded-boost variant (`returns >= 3` lifts one band) was also rejected
for introducing a threshold constant with no measurement behind it.

`returns` is recorded regardless, so the corpus re-run produces evidence for whether
recurrence should be promoted later. That is a one-line change if the data supports it.

### Components

| File | Change |
|---|---|
| `src/grill/triage.py` | Prompt: six intents, `concern` field. `VALID_SIGNALS`/`QUOTE_PROVABLE`/`CODE_GROUNDED` → `VALID_INTENTS`. `MOMENT_KEYS` gains `concern`. |
| `src/grill/merge.py` | **New.** Pure `merge(moments) -> list[MergedMoment]`. Groups, collapses, counts returns. No I/O, no LLM. |
| `src/grill/select.py` | `SIGNAL_RANK` → `INTENT_RANK`. `rank_key` → `(intent_rank, -returns, first_turn)`. |
| `src/grill/triage_run.py` | Report `intent`, `returns`, grouped concerns on the `also:` line. |
| stage 2 | Consumes `intent` to shape the question. Separate task. |

`merge.py` is separate from `select.py` because they answer different questions — "are
these the same concern?" versus "which concern wins?" — and merging is the part most
likely to need iteration after measurement. Both are pure and independently testable.

### Deleted, not deprecated

`new_pattern`, `explained_at_length`, `weak_evidence`, and the
`QUOTE_PROVABLE` / `CODE_GROUNDED` distinction. All exist to support code-grounded
signals stage 1 structurally cannot evidence. Retaining them preserves the illusion that
they might someday fire without a transcript change.

### Stage 2 contract

`TriageVerdict` gains `intent`, `concern`, `turns`, `returns`; `signal` and
`weak_evidence` are removed. Stage 2 branches on intent — a `definition_gap` earns a
different question than a `counter_proposal`. This is a breaking change to a consumer and
gets its own task.

## Measurement

Two gates, in order. Neither is optional.

**Gate 1 — stability (~$1).** 6 sessions × 3 runs. Two invariants:

1. The same quote receives the same intent across runs.
2. Moments that should group receive the same concern label across runs.

Concern-label drift is the sharper risk. If the LLM writes "volume tenancy" one run and
"storage tenancy" the next, groups split silently — ties return with no visible symptom.
If a pair of intents proves unstable, merge that pair before proceeding.

Gate 1 is new to this design. The equivalent check did not exist before, which is why the
original instability cost $5 to discover after the fact.

**Gate 2 — corpus re-run (~$3.50).** Full 69 sessions, then session-by-session human
judgement as before. Specifically checking whether `definition_gap` at rank 0 holds up,
and whether the merge produces concerns a human recognizes as single concerns.

## Known residual

- **The within-group representative is an unsolved judgement call.** "Best intent, then
  earliest turn" is deterministic, but "earliest turn" is the same recency question that
  scored 4/7 — moved inside the group rather than answered. It matters less there, since
  group members share a concern, but it is not solved.
- **`definition_gap` at rank 0 is the least confident decision here.** The one relevant
  data point (`341aa661`, "what is single-axis queries ?") was judged a correct keep,
  which supports it. One point is not much.
- **`asserts_belief` is untested.** It does not exist today; nothing in the corpus run
  validates it. It is an inference from reading quotes.
- **Cross-session dedup remains out of scope.** `99169aac` and `11e31f2a` are
  near-identical moments in two sessions. The concern merge is within-session only.
- **Paste contamination remains open.** The evidence rule verifies a quote is verbatim,
  not that the developer authored the thought.

## Rejected designs

**Few intents, sharply defined (3–4).** Lower wobble risk, but discards the distinction
between "what is X" and "why not Y" that motivated the change.

**Intent derived from quote syntax in code.** Fully deterministic — regex on "right ?",
"why not", "what is". Rejected as brittle on unseen phrasing, and it would put the
classifier's quality at the mercy of punctuation.

**LLM emits already-merged moments.** Simplest output, no grouping code. Rejected: it
returns the grouping judgement to the non-deterministic step.

**Flip the tiebreak to earliest turn.** All three judged losses preferred the earlier
moment, but recency still won 4 of 7. Net effect would have been −1.

## Revision history

- **2026-07-21, v1.** Written after the 69-session corpus run and hand review. Replaces
  the four-signal taxonomy from the 2026-07-17 design.
