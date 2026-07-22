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

## Flow

1. Run:

   ```
   grask serve --json
   ```

   If the output is `{"pending": null}`, say there is nothing pending and stop.
   If the command is not found, grask is not installed on this PATH — say so and
   stop rather than hunting for a checkout to `cd` into.

2. Ask ONE native question, preview-style (like plan-mode option picks), built
   entirely from the served JSON:

   - `question`: a context line from `topic` and `created_at`, then the full
     question text — e.g.
     `from 2026-07-21 · idempotency of the retry path — What would happen if …?`
     The question must be readable inside the picker itself; do not rely on
     markdown printed before it.
   - One option per stored option, in stored order:
     - `label`: the letter plus the first few distinguishing words of the
       option (e.g. `a) dedup to a no-op`). Keep labels short; they are not the
       full text.
     - `preview`: the full stored option text, verbatim and unabridged. The
       side-by-side preview pane is where the developer reads the option.
   - In exactly one option's preview (or the question text), append a footer
     note: "Other" accepts `skip`, or `wrong: <what's off>` if the question
     misreads what happened.

3. Record the result:
   - Picked letter L: `grask record <probe_id> --pick L`
   - Skipped: `grask record <probe_id> --skip`
   - Premise rejected: `grask record <probe_id> --wrong --objection
     "<their words>"` (omit `--objection` if they gave no reason).

   Show the result: ✓ or ✗ from `outcome`, then the `explanation` verbatim. If
   the command prints `{"error": ...}`, show the error and stop — do not retry
   with different flags.

4. Run `serve` again. If another probe is pending, say so and offer to
   continue — do not auto-serve it.
