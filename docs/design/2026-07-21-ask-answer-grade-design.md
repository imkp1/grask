# Ask / answer / grade — the `grill` CLI — Design

**Date:** 2026-07-21
**Status:** ready to plan
**Scope:** one probe, interrogated to a verdict, stored. Nothing resurfaces yet.

## Boundary

The capture pipeline can notice a moment, form a seed, and write a question. It then puts
that question in a table no one can read. This closes that end: a `grill` command takes one
pending probe, runs a criteria-driven interrogation to a verdict, and records what happened.

Deliberately out of scope:

- **Resurfacing.** `IDEA.md` calls it the half that matters most, and the second thing to
  build. It is the only consumer of the confidence numbers, and it needs its own spec.
- **Discovery.** Nothing pushes the question at you. You run `grill`.
- **The three-skip mute.**

That last one needs a reason rather than a shrug. "Skip three in a row and it goes quiet for
a week" is a rule about a tool that *pushes*. This one pulls: if you typed `grill`, you want
a question, and refusing to serve you because of last week's skips is hostile rather than
tactful. The rule belongs with the delivery surface that gives it meaning. So the data it
needs is recorded from day one and the rule is not implemented.

### The portal problem, named

`IDEA.md` killed the web portal with a specific argument: "the step where a human has to
volunteer input on a Tuesday is where every tool built on good intentions goes to die — so
there isn't one." A CLI you run yourself is that step, wearing different clothes.

This is accepted knowingly, on the grounds that it is temporary and unavoidable. Every
delivery mechanism worth considering — a SessionStart hook that injects the question, a
nudge that points at the CLI, the CLI alone — needs the interrogation loop to exist first.
Building the loop does not commit us to the pull model; skipping it would block all three.
Discovery is the next decision, and it is a decision about `cli.py`'s caller, not about
`ask.py`.

## Data flow

```
grill ─▶ cli.py ─▶ store.next_probe()  ──none──▶ "nothing to ask about." exit 0
                        │
                        ▼
                   ask.py loop ──▶ judge (per turn) ──▶ probe.followup (per gap)
                        │
                        ▼
                   store.record_ask(outcome, answers, criterion results)
```

## Probe selection

The newest unasked, unexpired probe. One per invocation.

Newest rather than oldest because the entire value proposition is a question about the code
you shipped this afternoon. Serving oldest-first is worst-first: it leads with the probe
whose session you have most thoroughly forgotten, which is the version of this tool that
feels like a quiz.

Probes expire seven days after `probes.created_at`. Expiry is computed, not stored, and
"unasked" is the absence of an `asks` row rather than a status column. Neither fact needs
maintaining, and `probes` gains no lifecycle column to fall out of sync.

One probe per invocation even when five are pending. The product is one question.

## 1. `ask.py` — the interrogation

Pure logic. Takes a probe and a console protocol (`show(text)`, `prompt(text) -> str`), with
`judge` and `followup` injectable exactly as `capture_session` injects its stages. No
argparse, no terminal control codes, no TTY. The whole loop is drivable in tests by a
scripted console at zero model spend.

The context line is one line of orientation above the question — when the session was, and
`seeds.topic` — so the developer knows which piece of work is being asked about before they
read the question.

```
context line + question
confidence   [95 | 70 | 40]   ·   enter = skip   ·   /wrong
answer                        ·   enter = skip   ·   /wrong
    ↓
judge(rubric, probe, accumulated_answer) → criteria[], passed, reasoning
    ↓
passed                   → show reasoning, done
turns == len(criteria)   → show reasoning, done (failed)
otherwise                → followup(first unmet) → back to the answer prompt
```

**Judging accumulates.** The `answer` handed to the judge is every developer turn so far,
concatenated in order — not the latest turn alone. Grading only the latest turn would mark
already-met criteria as unmet the moment the developer answers a narrow follow-up narrowly,
which would make the interrogation unlosable in the wrong direction: it could never
terminate on merit. Accumulating makes grading monotonic, and it leaves `judge()`'s
signature untouched — still one `answer` string. Only the return type moves.

**Confidence is asked once**, before the first answer, and never again. Committing a number
before answering is the whole mechanism: "you said 95%" is not an argument you can have with
a computer. Asking again at turn three is friction with no payoff, and the payoff it does
have arrives at resurfacing, which does not exist yet. The number is captured and not acted
on.

**Bounds.** Developer turns are capped at `len(criteria)`, which stage 3 constrains to 2–4.
Worst case is three follow-ups.

**Both exits are free.** `/wrong` and skip are live at every prompt, first question and
follow-ups alike, and neither spends a model call.

- **skip** — empty input. Outcome `skipped`. No judgement, no cost, no comment.
- **`/wrong`** — the premise is bad. Prompts once for an optional free-text objection, then
  ends. Outcome `premise_rejected`, stored as its own thing: not a pass, not a fail, not a
  skip.

That third outcome is the zealot defence made queryable. `IDEA.md` names a confident false
accusation about your own code as the failure that gets the plugin disabled forever, and it
is invisible unless counted. `premise_rejected / asked` is that rate, available from the
first interrogation. An escape hatch that required arguing your way out through the judge
would be the failure defending itself.

**Errors invert capture's rule.** `capture_session` never raises because it runs detached
with nothing watching. Here the developer is sitting in front of it, and `judge.py` already
states the principle: inventing a grade is worse than admitting the call failed. An
`LLMError` prints plainly and records outcome `error`. It never degrades to a pass.

## 2. `probe.py` — gains `followup()`

```
followup(question, answer, unmet_criterion) -> str
```

Question-writing lives in `probe.py`: the one-question rule, `ProbeRejected`, and the
`CORRECTION` retry that regenerates a rejected question rather than losing it. A second
module writing questions would be a second place to keep that rule correct, and the rule has
already been broken once in this repo.

The follow-up inherits the constraint that matters most — it must not contain its own
answer. A follow-up that leaks the criterion it is testing grades itself.

## 3. `cli.py` — the entry point

`grill`, registered in `pyproject.toml` alongside `grill-hook`. Argparse, real terminal IO,
and the console implementation `ask.py` is written against.

When no probe is pending it prints one line and exits 0. Silence is right for a tool that
pushes; a command you typed that prints nothing looks broken.

## 4. `judge.py` — per-criterion grading

`Verdict` gains a per-criterion breakdown; `passed` becomes `all(met)`. One judge, one
prompt, one place the coward defence lives.

| field | note |
|---|---|
| `criteria` | tuple of (criterion, met) in rubric order |
| `passed` | derived: `all(met)` |
| `reasoning` | unchanged — one or two sentences, addressed to the developer |

This is the riskiest change in the design, because it rewrites a component measured at
45/45. The risk is not that per-criterion grading is worse; it is that it could move in
either direction and both are plausible. Stricter, because every criterion becomes a veto
where the holistic judge could weigh them. More lenient, because each is assessed in
isolation and none carries the weight of the whole answer. Reasoning cannot settle which.

## 5. `storage.py` — three additive tables

Nothing existing changes.

### `asks` — one row per interrogation

| column | type | note |
|---|---|---|
| `id` | INTEGER PK | |
| `probe_id` | INTEGER FK UNIQUE | one ask per probe |
| `asked_at` | TEXT | ISO-8601 |
| `confidence` | INTEGER NULL | 95 \| 70 \| 40; NULL whenever the interrogation ended before an answer (skip or `/wrong` at the confidence prompt) |
| `outcome` | TEXT | `passed` \| `failed` \| `skipped` \| `premise_rejected` \| `error` |
| `objection` | TEXT NULL | free text from `/wrong` |
| `turns` | INTEGER | developer turns taken |
| `cost_usd` | REAL NULL | judge + follow-up calls summed |
| `completed_at` | TEXT NULL | |

`UNIQUE` on `probe_id` enforces one ask per probe, and is what makes "unasked = no `asks`
row" a correct query rather than a convention.

### `answers` — one row per developer turn

`id` PK, `ask_id` FK, `turn` (0-based), `question` (the probe at turn 0, the follow-up
after), `answer`, `created_at`.

Storing the question per turn rather than reconstructing it means a follow-up that read
badly can be found later. Follow-ups are generated, so they are the part most likely to be
wrong.

### `criterion_results` — one row per criterion per turn

`id` PK, `answer_id` FK, `criterion` TEXT, `met` INTEGER.

A table rather than a JSON column, and the capture spec's own rule is why. There, `quotes`
and `refs` are JSON *because* they are read back whole and never queried into. These are the
inverse: "which criteria fail most often" and "is any criterion never met by anyone" are the
queries that reveal whether stage 3 writes gradeable criteria at all. Same rule, opposite
answer.

### Derived, not stored

Consecutive skips — `select outcome from asks order by asked_at desc limit 3`. The mute rule
is deferred; the data it will need is not.

### Not yet

No `resurface_at`, no review history. Additive when resurfacing gets its spec.

## Testing (TDD)

**`ask.py`** — scripted console, injected judge and followup, no spend:

- passes on turn 1
- passes after one follow-up, targeted at the unmet criterion
- exhausts `len(criteria)` turns and records `failed`
- skip at the confidence prompt; skip at the answer prompt
- `/wrong` at each prompt, with and without an objection
- judge raises `LLMError` → outcome `error`, never a pass
- accumulation: turn 2's judge call receives both answers

**`storage.py`** — temp-db: round-trip an ask with answers and criterion results; the
`UNIQUE` constraint rejects a second ask; `next_probe` skips asked probes, skips probes
older than seven days, and returns the newest of the rest.

**`probe.followup`** — inherits the existing single-question and no-leaked-answer tests.

**`judge.py` — the calibration gate.** Re-run the saved 45-call suite from
`docs/measurements/2026-07-21-generated-rubric-calibration/` against the rewritten judge.
**45/45 is the merge condition**, roughly $1.07 since the probes and answers are committed
artifacts. Plus per-criterion assertions on the existing three fixtures.

**One real end-to-end interrogation**, marked `calibration`, against a stored probe. Worst
case is about four judge calls and three follow-ups; that needs a measured number rather
than an estimate, written to `docs/measurements/`.

## Wiring

`grill = "grill.cli:main"` in `[project.scripts]`. Editable reinstall. No settings.json
change — this is invoked by hand.

## Open

- **Stage 1 topic instability** still stands. The context line shown above the question is
  built from `seeds.topic`, so an arbitrary topic name is now visible to the developer
  rather than only present in the database. This is the first surface where that bug is
  something a human reads.
- **Discovery is undecided**, and is the next design question. `ask.py` is written to be
  indifferent to its caller so that decision stays open.
