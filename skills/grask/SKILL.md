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
  not in previews, not before the pick and not after it. Grading happens in
  `grask record`, and the only true thing you ever learn about the key is the
  `display` string it hands back. Relay that; never reason past it.
- One native question, one round. No confidence round, no follow-ups.

## Running grask

Do not assume a bare `grask` is on PATH — the plugin install deliberately does
not put one there. Call the shim the SessionStart hook writes, by its literal
path:

```
~/.claude/grask/grask
```

Keep it literal, and keep the arguments to the shapes below. grask ships a
`PreToolUse` hook that pre-approves exactly `serve` and `record` spelled this
way; anything else — a `${...}` expansion, an extra flag, a second command
joined on — falls through to a permission prompt, on every single call. On a
first `/grask` that is an opaque shell one-liner shoved in front of someone who
has not seen a probe yet.

Only if that path does not exist or will not run — a standalone install, or a
`GRASK_HOME` pointing elsewhere — fall back to the resolver, once:

```
GRASK="${GRASK_HOME:-$HOME/.claude/grask}/grask"; [ -x "$GRASK" ] || GRASK=grask; "$GRASK" <args>
```

Where a step below writes `grask …`, run `~/.claude/grask/grask …`, or the
resolved `"$GRASK" …` if you had to fall back.

## Flow

1. Run:

   ```
   ~/.claude/grask/grask serve --json
   ```

   `pending` is `true` when a probe is waiting and `null` when none is. Both
   arms carry the key, so branch on it rather than on which other fields
   happen to be present.

   If `pending` is null, the queue is empty: relay the `note` field from that
   same JSON and stop. Do not shorten it to "nothing pending" and do not
   generalise across the `reason` codes — `note` already says *why* it is empty,
   and the six reasons (`never`, `caught_up`, `capturing`, `unverified`,
   `expired`, `over_cap`) mean different things. `over_cap` especially: those
   probes are still waiting, they just do not fit this UI, so the note sends the
   developer to the terminal. `capturing` is the other one worth getting right: a
   session just ended and its question is still being written, so the queue is
   not empty but early — relay the note and do not suggest ending another
   session. `unverified` is the opposite case and must not be softened into
   "caught up": grask wrote a question, could not confirm its answer key, and
   threw it away rather than grade the developer against a key that might be
   wrong.
   If `grask` cannot be found or run either way, grask is not installed here —
   say so and stop rather than hunting for a checkout to `cd` into.

2. Ask ONE native question, preview-style (like plan-mode option picks), built
   entirely from the served JSON:

   - `question`: a neutral provenance line from `created_at` alone, then the
     full question text — e.g. `from 2026-07-21 · What would happen if …?`.
     The question text carries any file names it needs. The question must be
     readable inside the picker itself; do not rely on markdown printed before
     it. There is nothing else in the payload to add: `serve` deliberately does
     not send the probe's topic, because the topic states *why* the probe was
     raised and that rationale is the bridge to the graded answer. It arrives
     in step 3's `display`, once the answer is settled and it can no longer
     leak the mechanism under test.
   - `header`: a short constant chip — use `Probe`.
   - One option per stored option, in stored order. Every option needs all
     three fields below; `label` and `description` are required by the question
     tool's schema, and omitting `description` fails the call outright:
     - `label`: the letter plus the first few distinguishing words of the
       option (e.g. `a) dedup to a no-op`). Keep labels short; they are not the
       full text.
     - `description`: the next clause or so of the same stored option text,
       carrying on from where the label stopped, then cut off. It is a longer
       mechanical excerpt and nothing else — never a gloss, a summary, or a
       hint about whether the option holds.
     - `preview`: the full stored option text, verbatim and unabridged. The
       side-by-side preview pane is where the developer reads the option.
   - In exactly one option's preview (or the question text), append a footer
     note: "Other" accepts `skip`, or `wrong: <what's off>` if the question
     misreads what happened.

3. Record the result (through whichever path worked in step 1):
   - Picked letter L: `~/.claude/grask/grask record <probe_id> --pick L`
   - Skipped: `~/.claude/grask/grask record <probe_id> --skip`
   - Premise rejected: `~/.claude/grask/grask record <probe_id> --wrong
     --objection "<their words>"` (omit `--objection` if they gave no reason).

   Print the `display` field verbatim, as markdown. It is already the whole
   result — verdict, the correct option where there is one, the explanation,
   and the withheld provenance, now that the answer is settled and the topic can
   no longer leak it. Do not restate it, summarise it, add a verdict of your
   own, or comment on how the answer went. You did not grade it and you still
   have not seen the key except in that string.

   If the command prints `{"error": ...}`, show the error and stop — do not
   retry with different flags.

4. In that **same reply**, without waiting for a turn of your own, read the
   `next` field of the `record` output you just used. Do NOT run `serve` again
   — `next` already holds exactly what `serve` would have printed.

   Everything in a `/grask` round that is not the developer reading or tapping
   is you taking a turn, and a turn is seconds against 60ms of actual work.
   Printing the result and offering the next probe are one reply, not two.

   - `next.pending` is null: relay its `note`, exactly as in step 1, and end.
   - Otherwise a probe is waiting. Ask a native yes/no question — `Serve the
     next probe?` with options `Yes` and `Stop` — rather than a prose offer, so
     the continue step matches the probe's own picker affordance and per-probe
     consent stays an explicit tap. Do not auto-serve, and do not show any part
     of the next probe inside that yes/no question. On `Yes`, go to step 2
     building the picker from `next` (it is the served payload — no further
     command needed); on `Stop`, end.
