---
name: grask
description: Serve the next pending grask probe — a multiple-choice question about code the developer recently shipped — using the native question UI. Use when the user types /grask.
---

# Serving a grask probe

Grask quizzes the developer on mechanisms they shipped without fully
understanding. You are the delivery surface only: you serve the question and
relay the answer. You never grade, never guess, and never see the answer key.

## Hard rules

- Never open or query grask's database directly. The two subcommands below are
  the entire interface.
- Never speculate about which option is correct — not in text, not in labels,
  not in previews, not before or after the pick. Grading happens in
  `grask record`.
- One native question, one round. No confidence round, no follow-ups.

## Running grask

Do not assume a bare `grask` is on PATH — the plugin install deliberately does
not put one there. Every `grask` call below goes through this resolver, which
prefers the shim the plugin's SessionStart hook writes and falls back to a
PATH `grask` for the standalone install:

```
GRASK="${GRASK_HOME:-$HOME/.claude/grask}/grask"; [ -x "$GRASK" ] || GRASK=grask
```

Prepend that to each command, so every call is self-contained (a new shell each
time). Where a step below writes `grask …`, run `"$GRASK" …`.

## Flow

1. Run:

   ```
   GRASK="${GRASK_HOME:-$HOME/.claude/grask}/grask"; [ -x "$GRASK" ] || GRASK=grask; "$GRASK" serve --json
   ```

   If the output is `{"pending": null}`, say there is nothing pending and stop.
   If `grask` cannot be found or run either way, grask is not installed here —
   say so and stop rather than hunting for a checkout to `cd` into.

2. Ask ONE native question, preview-style (like plan-mode option picks), built
   entirely from the served JSON:

   - `question`: a neutral provenance line from `created_at` alone, then the
     full question text — e.g. `from 2026-07-21 · What would happen if …?`.
     Do NOT put `topic` in the picker: the topic states *why* the probe was
     raised, and that rationale is the bridge to the graded answer — showing it
     pre-answer leaks the mechanism under test. The question text carries any
     file names it needs. The question must be readable inside the picker
     itself; do not rely on markdown printed before it. Hold `topic` back for
     step 3.
   - One option per stored option, in stored order:
     - `label`: the letter plus the first few distinguishing words of the
       option (e.g. `a) dedup to a no-op`). Keep labels short; they are not the
       full text.
     - `preview`: the full stored option text, verbatim and unabridged. The
       side-by-side preview pane is where the developer reads the option.
   - In exactly one option's preview (or the question text), append a footer
     note: "Other" accepts `skip`, or `wrong: <what's off>` if the question
     misreads what happened.

3. Record the result (through the same `$GRASK` resolver as step 1):
   - Picked letter L: `"$GRASK" record <probe_id> --pick L`
   - Skipped: `"$GRASK" record <probe_id> --skip`
   - Premise rejected: `"$GRASK" record <probe_id> --wrong --objection
     "<their words>"` (omit `--objection` if they gave no reason).

   Show the result: ✓ or ✗ from `outcome`, then the `explanation` verbatim,
   then the withheld provenance — `This came up from: <topic>` — now that the
   answer is settled and the topic can no longer leak it. If the command prints
   `{"error": ...}`, show the error and stop — do not retry with different
   flags.

4. Run `serve` again. If another probe is pending, ask a native yes/no question
   — `Serve the next probe?` with options `Yes` and `Stop` — rather than a
   prose offer, so the continue step matches the probe's own picker affordance
   and per-probe consent stays an explicit tap. Do not auto-serve. On `Yes`,
   go to step 2; on `Stop`, end.
