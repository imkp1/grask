# Multiple-choice probes

2026-07-22. Replaces free-text interrogation with multiple choice, and steers
probe generation technical.

## Problem

Two complaints from first real use:

1. **The interaction is too bad.** Typing a prose answer at a terminal prompt is
   more than anyone will do at the moment grill catches them. The free-text →
   judge → follow-up loop also costs a judge call per turn and seconds of
   latency while a human waits.
2. **Questions are behavioural, not technical.** The first live probe opened
   "You asked why the PR body didn't say which issue was being fixed…" — a
   retelling of the conversation. The thing worth testing was the mechanism
   (GitHub only autolinks `#N` after a non-word character).

## Decisions

Made with the user, 2026-07-22:

- **Pure multiple choice.** Picking an option IS the answer. No judge call, no
  follow-ups, no typed reasoning. Verdict is mechanical.
- **Technical at both selection and stem.** Triage/seed prefer moments with a
  technical mechanism at their core, and the question stem asks about the
  mechanism directly — never a retelling of the conversation.
- **Confidence commit survives.** 95/70/40 before the pick, as today. The
  miscalibration signal (confident + wrong pick) is the product.

## Design

### Stage 3 (`probe.py`)

The prompt returns one JSON object:

```json
{"question": "...", "options": ["...", "..."], "correct": 0, "explanation": "..."}
```

- `question` — one question, mechanism-first. Stem rules: ask what the API /
  tool / config / algorithm does or requires; no "you asked", "you said",
  "Claude did", or any conversational narrative; still grounded in the
  session's actual artifact (name the file, flag, or identifier that shipped).
  The existing one-question gates (question-mark count, second-question regex)
  are kept.
- `options` — 3 to 5 one-line options, exactly one correct. Distractors are
  plausible wrong *mechanisms* — the job the old "name a plausible WRONG answer
  to rule out" criterion did now lives in the distractors. No "all of the
  above" / "none of the above".
- `correct` — index into `options` as generated.
- `explanation` — 1-3 sentences shown after the pick, right or wrong: why the
  correct option is correct, in terms of the mechanism.

Structural gates (rejection → retry with correction, as today, `MAX_ATTEMPTS`
unchanged): 3–5 options; options pairwise distinct after strip; `correct` a
valid index; `explanation` non-empty; existing compound-question gates on the
stem. `criteria` disappears from the stage-3 output; `Rubric.criteria` and the
criteria-count turn cap go with it.

Options are **shuffled before storage** (storage-time, not display-time, so the
stored row is the single source of truth for what position was shown and
`correct_idx` is stored post-shuffle).

### Selection (`triage.py` / `seed.py` prompts)

Add one stated preference to the moment-selection prompts: prefer moments whose
core is a technical mechanism (API semantics, tool behavior, data formats,
configuration effects, algorithms); down-rank behavioural or process moments
(why a message was phrased a certain way, workflow or etiquette choices, why
the assistant took an approach). Prompt-only change; no new gates. Stage-1
topic instability is a known open issue and is not addressed here.

### Ask flow (`ask.py`)

```
from 2026-07-21 · Linking a PR to the issue it fixes
What did swapping `GH#4923` for `Refs #4923` cause GitHub to create?
  a) A closing keyword that closes #4923 on merge
  b) A cross-reference event on #4923's timeline
  c) A label linking the PR to the milestone
confidence   [95 | 70 | 40]   ·   enter = skip   ·   /wrong
> 95
pick   [a-c]   ·   enter = skip   ·   /wrong
> b
✓ GitHub only autolinks `#N` when `#` follows a non-word character …
```

- Prompt 1: confidence, exactly as today (enter = skip, `/wrong` →
  premise-rejected with optional objection, invalid input reprompts).
- Prompt 2: letter pick. Enter = skip, `/wrong` as above, invalid input
  reprompts with a hint. Case-insensitive.
- Verdict: pick == `correct_idx` → `passed`, else `failed`. Explanation is
  shown either way, prefixed ✓/✗. `reasoning` stores the explanation.
- No LLM call anywhere in the ask path. `outcome = error` survives only for
  malformed stored rows (e.g. options fail to parse).
- `judge()` and `followup()` leave the ask path. `judge.py`'s `Verdict` /
  `CriterionResult` and `probe.py`'s `Followup` / follow-up prompt are deleted
  along with the `criterion_results` table writes; `Rubric` shrinks to
  topic + hypothesis. Delete rather than strand: dead grading machinery in a
  measurement tool is a standing invitation to misread the numbers.
- `Interrogation.turns` collapses to at most one turn (the pick, stored as the
  answer text of the chosen option). The `asks` schema keeps `turns` for
  continuity; it is 0 (skip before pick) or 1.

### Storage (`storage.py`)

- `probes` gains `options` (JSON TEXT), `correct_idx` (INTEGER),
  `explanation` (TEXT). Migration runs at open (`ALTER TABLE`, nullable
  columns).
- `next_probe` adds `AND p.options IS NOT NULL`: legacy free-text probes are
  never served. They age out via the existing TTL.
- **Backfill, one-off:** the single pending probe (id 1, GH#4923) gets options
  minted by one LLM call that converts its stored criteria into
  options/correct/explanation, run through the same structural gates, then
  written to the new columns. If the call fails, the probe stays legacy and
  ages out — no retry loop beyond `MAX_ATTEMPTS`.

### Testing

- `ask.py` remains console-injected pure logic: scripted-console tests for
  correct pick, wrong pick, skip at each prompt, `/wrong` at each prompt,
  invalid-then-valid input, malformed stored options → `error`. Zero spend.
- `parse_probe` gates unit-tested per rule (count, duplicates, index range,
  empty explanation, compound stem).
- Shuffle tested by property: stored `correct_idx` always names the same
  option text the model marked correct.
- Migration tested against a copied legacy DB fixture: columns appear, legacy
  row not served by `next_probe`.

### Out of scope

- Discovery/delivery (hook vs CLI nudge) — still undecided, unchanged.
- Stage-1 topic instability.
- Per-turn cost breakdown (moot: the ask path no longer spends).
