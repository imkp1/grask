# grask — Design

How grask works and why each part is shaped that way. This is the current document: when
the code and this disagree, one of them is a bug. The revision history at the end records
what changed and why.

## Premise

You cannot tell the difference between understanding something and having watched it
happen. Reading about idempotent retries and nodding along feels identical, from the
inside, to knowing it. Watching an agent apply a pattern across three PRs feels like
learning it.

grask finds out which one it was, by asking questions you can't bluff.

## Goals

- **Primary:** surface blind spots in how the user engineers — gaps they cannot self-report
  because they don't know they have them.
- **Secondary:** counter staleness. Concepts that keep recurring in real work but grade
  hollow are the highest-value output of the system.
- **v2 addition:** be used without willpower. A developer must encounter grask because they
  coded, not because they remembered it existed.

## Users

Developers who write code with Claude Code. Not a single-user tool. This is a change from
v1 and it is the change everything else follows from: a tool one known user tolerates is
not a tool strangers install twice.

## Non-goals (v1)

Auth. Multi-user accounts. Content library. Lesson authoring. Streaks, scores, XP,
notifications. FSRS. Cursor capture. Mining git history or PR review comments. Web portal.
Weekly report.

Each was considered and cut. Rationale for the load-bearing cuts is in "Rejected designs".

## Interaction model

**Trigger-based, attached to work the developer already does.** grask fires at the end of a
Claude Code session — the one moment in the day when the topic is loaded in the developer's
head rather than only in a database.

The system never asks the developer to go anywhere. There is no destination.

Price of an encounter: **one question, ~20 seconds.** Depth is opt-in, offered only after
the developer is already engaged and already caught.

**The trigger is free and the price is 20 seconds.** That is the whole economic argument for
the product, and every mechanic in this design either protects it or is cut. It is why there
are no streaks (they buy a habit the trigger already gives), why the cap is one question
(the price is the promise), and why silence is a first-class outcome (a question with
nothing behind it spends the 20 seconds and returns nothing). When a proposed feature raises
either half, that is the reason to reject it.

Many sessions produce no question at all. See "Silence is a valid outcome".

### Why not pull-based (reversal of v1)

v1 was pull-based: a portal the user opened on purpose. It rejected session-end prompts on
the grounds that *"a prompt at the end of every session gets reflexively dismissed by week
two, and then the plugin gets disabled."*

That reasoning is sound about the failure mode and wrong about the alternative. It compares
a flawed option against the portal — but the same document concludes the user will not open
the portal (*"The user will not show up daily. This is a stated constraint, not a hope"*).
The real comparison was flawed-prompt vs. nothing, and it chose nothing.

The dismissal risk is real and is priced against, structurally, in "Restraint" below. The
v1 rejection assumed a session-end grask costs 5–8 questions. It costs one.

## Architecture

Two components. One language. No server.

```
Claude session ends
        │
        ▼
   Extractor ──▶ Topic ──▶ Core (grask) ──▶ Verdict ──▶ terminal
                              ▲                          │
   `grask <topic>` ───────────┘                          │
   (manual / test path)        └──── resurface ──────────┘
```

### 1. Core (`grask`)

Takes `Topic(name, context?)`, runs an adversarial interrogation, produces a Verdict.

Knows nothing about Claude, sessions, or plugins. Its entire input is a topic. This
isolation is what makes the risky part testable with no capture pipeline attached.

### 2. Extractor

Reads the session transcript at `SessionEnd`, picks one topic, hands it to the core.
Never blocks, never prompts beyond the single question, never speaks on failure.

### State

SQLite. Topics, occurrences, rubrics, transcripts, verdicts, resurface dates. No UI.

### Entry points

1. **`grask <topic>`** — CLI. Ships first, always works, needs no transcript. This is both
   the manual path and the test harness for the core.
2. **`SessionEnd` hook** — the product.

## The questioning engine

The project succeeds or fails here. Unchanged from v1 except where noted.

### Central risk: the question might not be worth asking

**The question is the product. Everything else is support.** A good question with a
mediocre judge is still worth twenty seconds — the developer stops, thinks, and finds their
own hole whether or not the grader scores it well. A bad question with a perfect judge is
an uninstall: *"what is a retry policy"* tells the developer instantly that this thing has
nothing to teach them, and no downstream quality recovers that.

So the bar is not "a relevant question." It is a question that makes an experienced
engineer stop for thirty seconds and go *"…huh"* — every session, from their own code.
Whether an LLM clears that bar reliably is the bet this project is making, and it is
validated first, ahead of the judge. It is measured, not argued: see "North-star metric".

Two earlier revisions named the coward judge as the central risk. That ranking has now been
overtaken twice — once by developer motivation (see "Open questions") and once here — and
the section title was the last thing still asserting it. `IDEA.md` has led with question
quality since v2.1; this is that correction reaching the design.

**Rubric quality is the same risk seen from the other end.** The rubric decides what a
correct answer contains; the probe is the instrument that tests for it. A shallow rubric
cannot produce a sharp probe, and a sharp probe against a wrong rubric fails the user for
the model's mistake. Both are addressed by the Hypothesis below, which gives the rubric
something to be derived *from* rather than invented against a bare topic name.

### Secondary risk: LLMs are cowards

An LLM asked to grade an explanation will say it's great. It will accept vagueness, fill
gaps on the user's behalf, and call it correct. **A grask that doesn't grask is worse than
nothing** — it manufactures the exact false confidence the system exists to destroy.

Secondary to probe quality, not minor: this is still the failure that turns the tool into
an expensive machine for feeling smart. It ranks below question quality only because a
cowardly judge attached to a sharp question is survivable, and a perfect judge attached to
a dull one is not.

The mitigations are structural. Prompting a model to "be harsh" is not a control.

**1. Asker and judge are separate calls.** The interrogator never grades. The judge never
sees the conversational frame — it receives answers as cold evidence. A model that spent
five turns being encouraging cannot credibly fail the user afterward.

**2. Rubric before questions.** On topic arrival, the system generates what a correct
understanding *contains* — key claims, standard misconceptions, tradeoffs — **before
seeing any user input**. The rubric is then frozen. Grading becomes "did they hit these
claims," not "did that feel smart." Without this the judge grades fluency, and the user is
fluent. That is the trap.

**3. The judge must quote.** Every verdict cites the user's actual words. No quote, no
verdict. Kills vibes-based grading and makes verdicts contestable.

**4. Probes are grounded in the user's own code.** For each rubric claim, the question is
generated against the artifact from the session — the actual wrapper, the actual interface
— not the concept in the abstract. Not *"what is a retry policy"* but *"here is your retry
wrapper from today; what happens if the first attempt succeeded and the response got lost?"*

A question answerable from memory grades memory. The probe must be one the user could only
answer by understanding the code they shipped. This is what turns the why-not, the edge
case, and the tradeoff into something un-bluffable rather than trivia.

**Cost, stated plainly:** this makes control 2 harder, not easier. See "Known ceiling".

### The Hypothesis

Between the moment and the rubric sits a **falsifiable claim about what the developer
accepted without understanding.** Not a topic label — a sentence that can be wrong:

```
moment      "why do we need the idempotency key here?"  (turn 14)
concern     retry safety
hypothesis  The developer accepted the retry wrapper's safety argument without
            understanding that at-least-once delivery makes the first attempt's
            outcome unknowable.
```

grask was already doing this and not naming it. The object appeared twice under different
names: as `concern` in stage 1, which is a noun phrase and therefore untestable, and as the
requirement that an artifact-grounded rubric **"record the inference it made about the
code"** — framed defensively, as a debugging aid for when the rubric is wrong. Those are
the same object. Naming it once does four things:

- **The rubric becomes derived rather than invented.** The rubric is what would have to be
  true for the hypothesis to be false. Generating a rubric from a bare topic name is why
  "rubric quality caps everything" reads as an unsolvable ceiling; generating it from a
  stated claim gives it something to be accountable to.
- **It scopes the verdict.** The verdict is on the hypothesis, not the topic. See "Verdict
  scope".
- **"Your premise is wrong" gets a target.** Today it is a vague right to contest. Against
  a stated hypothesis it is a clean refutation of a specific claim, which is also what makes
  it loggable as a bug report against grounding.
- **It makes a "not worth asking" vote diagnosable.** A no-vote on a question says nothing.
  A no-vote against a recorded hypothesis says which half broke: wrong hypothesis, or right
  hypothesis tested by a bad question. Those need opposite fixes. This is what makes the
  north-star metric actionable rather than merely collectible.

**The hypothesis is internal. It is never shown as the framing of the question.** "You
copied this without understanding idempotency" stated to a developer's face is precisely
the confident accusation this design refuses to make — the one that gets the plugin
disabled forever. The system asks; it does not accuse. The hypothesis drives the rubric and
the probe, is inspectable on demand alongside the rubric, and never becomes the greeting.

It is also the riskiest object here, because it is the hallucinated premise from "Known
ceiling" given a name. Naming it does not reduce that risk — it makes it inspectable, which
is the mitigation the design already wanted and had no place to put.

### Loop

```
Moment ──▶ Hypothesis ──▶ Rubric (frozen)
                              │
                              ▼
   Sharpest single probe ──▶ answer | skip | "your premise is wrong"
            │
            ▼
   Judge (cold: rubric + answer) ──▶ claim verdict
            │
            ├── solid  ──▶ done. silent.
            └── hollow ──▶ gap + where to look
                              │
                              ▼
                     "go deeper?" ── yes ──▶ 5–8 probes, budget hard stop
                              │
                              ▼
                        resurface scheduled
```

### Verdict scope: claim-level, accumulating

**One probe grades one rubric claim, not the topic.** A single answer is thin evidence and
the design does not pretend otherwise. A topic's verdict accumulates across recurrences.

**One question can identify one misconception. It cannot identify understanding.** These
are not the same thing and the distinction has to survive into the user-visible strings,
not just the prose here. Everything grask can honestly say after one probe is *"one
important claim within this topic appears hollow"* — never *"you don't understand retries."*
Language that scopes a verdict to a topic on one answer's evidence is a false accusation
with extra steps, and it is the exact overreach that gets the tool disabled. Earlier
versions of this design were careful in prose and sloppy in the verdict format, which is the
only place the developer actually reads.

This follows from v1's own observation — *"Recurrence is signal: a concept that keeps
returning and grades hollow is the highest-priority item in the system."* v1 counted
occurrences to sort a queue. v2 compounds them into the verdict, and uses them to pick the
next probe: a recurring topic gets questioned on a claim it hasn't been questioned on yet.

Opt-in deep sessions grade the topic directly, on the v1 budget of 5–8 questions.

### Budget

Default: 1 question. Opt-in deep session: 5–8, hard stop. An unbounded interrogation is one
the user quits, and a quit session yields no verdict.

### "I don't know" is free. So is skipping.

An honest IDK ends that thread immediately, with no penalty. If admitting ignorance feels
like failure, the user bluffs, and the system grades the bluffing.

Pressing Enter without answering is always valid and carries no penalty, for the same
reason. A skip that costs something produces a bluff or an uninstall.

This is a hard design constraint. The system depends on the user being willing to be wrong
in front of it.

### Payoff: the gap, plus where to look

A verdict that only names the hole is a diagnosis. Hollow verdicts also point at the
specific thing that closes the gap — the doc section, or the code in the user's own repo
where the pattern is already load-bearing.

This is aiming, not teaching. Lesson authoring stays cut: *"any chat window explains
idempotency in fifteen seconds. The scarce thing is knowing it's your problem, with
receipts."* The payoff is knowing which fifteen seconds are yours.

### Verdict format

```
retry safety (3rd time this has come up)

This claim looks hollow:
  a retried charge won't double-bill because the provider handles it
  > "the retry would go through again, but stripe handles that?"
  (rubric: at-least-once delivery means the wrapper cannot assume the
   first attempt failed; safety comes from an idempotency key, not
   from the provider)

Look: payments/client.py:47 — your wrapper, no idempotency key

Asked 2 claims here so far · 1 hollow, 1 solid · back in 3 days
```

**The graded noun is a claim, never the topic.** The topic is a heading and a scheduling
key; it never takes an adjective. `Verdict: hollow` on a line beginning `Topic:` is banned —
that string asserts something one probe cannot support.

Verdicts are `solid` | `partial` | `hollow`. No scores, no percentages, no XP — false
precision invites optimizing the number instead of the understanding. The accumulation line
is a count of claims, not a grade on the developer.

### Known ceiling

**Rubric quality caps everything.** A shallow or wrong rubric makes every downstream
verdict confidently wrong. Not solvable in v1. Mitigated by making rubrics stored and
inspectable: when a questioning feels off, inspect the rubric first.

**Grounding probes in real code raises the stakes of that ceiling.** A rubric for a concept
is generated from world knowledge; it is wrong only if the model is ignorant. A rubric for
*this interface in your codebase* requires the model to have correctly inferred why the
interface exists — and when it infers wrong, the judge fails the user against a rubric that
is simply mistaken, with quotes, confidently. That failure mode is worse than a coward
judge: it is a confident judge grading a hallucinated premise.

Mitigations:

- An artifact-grounded rubric **records the inference it made about the code**, in the
  user's view, not just the claims it expects.
- **"Your premise is wrong" is a valid answer.** It ends the thread like an IDK, with no
  penalty, and flags the rubric. Contesting the rubric is never failing.
- A high rate of contested rubrics is a bug report against grounding, and the fallback is
  concept-level probes for that topic.

## Restraint

The v1 rejection of session-end prompts was right about how push-based tools die. Three
structural limits, none of them tonal:

**One question per session, hard.** No second question unless the developer asks for one.

**Skipping is free.** See above. Enter is always a valid answer.

**Three consecutive skips → silent for a week.** No "we miss you," no re-engagement copy.
The tool notices it isn't wanted and leaves. This is the only retention mechanic in the
design and it works by backing off.

Still absent: streaks, XP, scores, notifications, daily review. The habit comes from the
trigger being free and the price being 20 seconds.

## North-star metric

After the question — and after the verdict, so it costs nothing in the moment it would
distort — grask asks one thing:

```
was this worth asking?   [ y / n ]
```

**Yes-rate is the north-star metric.** If grask consistently earns a yes, almost everything
else becomes tractable; if it doesn't, no amount of judge tuning, taxonomy work, or capture
engineering matters. It is the direct measurement of the central risk named above, which
until now had no instrument at all.

**It replaces skip rate as the quality signal.** Skip rate was the only feedback channel in
this design ("high skip rate is a bug report against the extractor"), and it conflates *bad
question* with *busy developer* — two things needing opposite responses, which it cannot
distinguish. A binary asked after the answer separates them.

**It is diagnosable because the hypothesis is recorded.** A no-vote is logged against the
hypothesis, the intent, and the probe, so the failure is attributable: wrong hypothesis
(triage found nothing real), or right hypothesis and bad probe (stage 3 asked it poorly).
Without the Hypothesis object a no-vote is an unactionable complaint.

**It replaces hand-judging as the evaluation instrument.** Taxonomy and ranking changes are
currently validated by a $3.50 corpus run plus hours of the author judging moment selection
in isolation — judging a stage whose output nothing consumes. Once yes-rate exists, those
changes are evaluated by their effect on it, which is both cheaper and measures the thing
that actually matters.

Constraints, so the metric does not violate the design that produced it: it is one keypress,
it is skippable like everything else, it is never framed as feedback *on the developer*, and
it never asks why. A follow-up "what was wrong with it?" is the same nagging this design
rejects everywhere else.

## Capture

Claude Code `SessionEnd` hook. Reads the transcript, extracts one candidate, questions, exits.
Fails silently.

### Staged, in one invocation

The hook does three separable things. They are separate prompts with separately stored
outputs, but they run in the same invocation with the same transcript loaded.

**Stage 1 — triage.** Every session, no exceptions. Is there anything here worth asking
about? A session of config edits, dependency bumps, or a bug the user drove themselves
exits here, writing nothing — no state, no nudge. This is "Silence is a valid outcome"
made into a pipeline step rather than a hope. Expect most sessions to stop here.

**Stage 2 — seed.** Only if triage says yes. Extracts the topic, the verbatim user quotes,
the file:line refs, the specific decision that shipped unexamined, and **the hypothesis** —
the falsifiable claim about what was accepted without understanding. Stored as its own
record. The hypothesis is the seed's most important field: it is what stage 3 derives the
rubric from, and what a "not worth asking" vote is later attributed to.

**Stage 3 — question + rubric.** Reads the full transcript, not just the seed. Derives from
the hypothesis what a correct understanding contains — per "Rubric before questions", frozen
before any user input exists — then produces the probe that tests it.

**Why one invocation.** The transcript is the expensive input and the fragile one:
transcript files rotate, and the diff a seed references drifts as the branch moves. Reading
it once, at the moment it is freshest, is worth more than the work saved by deferring.

**Why stage 3 reads the transcript and not the seed.** A compressed seed is enough to name
the topic; it is not enough to quote the user back to themselves, which controls 3 and 4
depend on. Compressing here would cap probe quality to save tokens on the cheapest stage by
input size. If it later turns out a seed *is* sufficient, stage 3 becomes lazy — deferred to
`grask` invocation — with no other change to this design.

**Why the seed is stored anyway.** A stored seed makes questions regenerable. When the
stage-3 prompt improves, every past seed can be re-run into a better probe without needing
transcripts back. Given that probe quality is the top engineering risk, the ability to
re-ask the whole corpus after a prompt change is worth the storage.

### Measured cost (2026-07-20, 107-session corpus)

This section previously deferred a set of optimizations against "a full transcript read."
Measurement showed that read barely exists.

```
107 sessions      290 MB raw  ->  80 KB extracted   (3561x)
mean human input             0.8 KB / session
sessions with no human turn  41%
sessions that edit files     40%   (43 of 107)
code touched, median         36 KB / session
code touched, worst         191 KB / session
```

Consequences:

- **Stage 0 is free.** Deterministic filtering, no model, and it alone decides 41% of
  sessions produce nothing. The "heuristic triage to save money" optimization is already
  built — it is stage 0 — and stage 1 only ever sees sessions with real human input.
- **Transcript size is not a cost driver.** 0.8 KB of prompts per session cannot justify a
  cheaper model for stage 2. Extraction runs on the user's model because the savings from
  downgrading it are not measurable.
- **Code is the only real input cost**, at ~10k tokens for a median session and ~50k worst
  case, and only on the 40% of sessions that touch files.
- **The stage-3 question is settled by measurement, not tradeoff.** "Reads the transcript,
  not the seed" was argued above as quality-vs-cost. At 0.8 KB there is no cost side. Pass
  everything.

The one optimization still worth having is **diff rather than whole files**. The figures
above count entire current files, which is the naive strategy; the session's actual diff is a
fraction of that and is what a grounded probe needs anyway. Deferred until probe quality is
known, because a probe may well need surrounding context that a diff omits.

Lazy stage 3 stays rejected on grounds unrelated to cost: it would only produce questions for
sessions someone chose to open, a biased sample of the one thing most in need of unbiased
measurement.

### What counts as a topic

The governing question, from which everything else follows:

> **What evidence suggests the developer accepted something without fully understanding it?**

The taxonomy below is a set of detectors for that invariant. It is subordinate to it. If an
intent stops serving the invariant, the intent goes — it does not get to redefine the
principle by being the thing that happens to be implemented.

Moments the user *engaged with*, classified by intent and ranked. The ranking below is the
current one; `triage.py` is where it is enforced.

1. **`definition_gap`** — "what is X" — clearest gap, and the explanation is unverified
2. **`asks_rationale`** — plain "why X" — they took a rationale on faith
3. **`counter_proposal`** — "why not Y" — they had an alternative and dropped it
4. **`asserts_belief`** — "…right ?" — stage 1 cannot tell if it was confirmed or corrected

The invariant *derives* this order rather than asserting it: all four are acceptance-shaped,
strongest evidence of acceptance first. The shorthand is **quiz what they were told, not
what they told the agent** — a question they asked means an answer they received that
nobody checked.

**Drop candidates, pending measurement.** Two intents survive from an earlier list and do
not satisfy the invariant:

- **`contradiction`** — they cited evidence against the agent and were right. They accepted
  nothing. There is no unexamined acceptance here to probe.
- **`pushed_back`** — they overrode and the agent complied. Weaker case for dropping: they
  may have accepted the agent's compliance as agreement. But nothing was explained to them,
  so there is no received explanation to test.

Both currently rank last, so they should rarely or never win selection. The corpus re-run
records whether either ever wins. **If neither wins a session, they are deleted** — the same
disposal `new_pattern` and `explained_at_length` received, and for a stricter reason: those
two were unreachable, these two are reachable and off-principle. Keeping an intent that
cannot express the invariant is how the taxonomy quietly becomes the principle.

Ranking is enforced in `select.py`, not prompted for. An earlier version of this section
ranked "explained at length" and "newly introduced patterns" second and third; both were
deleted after measuring zero and one hits across 69 sessions. They are defined as
code-grounded, but stage 1 sees only user turns, so the evidence rule makes them
unreachable.

Explicitly not: files edited, commands run, bugs fixed. That is activity, and activity is
not learning.

**Guard on `definition_gap`.** Ranking it first names the *moment*, never the question. The
question must still be grounded in the code the user shipped — "your ranker does X, what
happens when Y", never "define single-axis queries". A bare definitional question is the
failure IDEA.md calls unrecoverable, and rank 1 is where that failure would enter.

### Cap: 1 topic per session

Down from v1's 2–3. Same reasoning one notch harder: a topic is used now, not stored, so
there is no backlog to fill. Missing topics is acceptable — they recur if they matter.

### Silence is a valid outcome

**If no candidate clears the bar, grask says nothing.** A session of boilerplate, config
edits, dependency bumps, or a bug the user drove themselves produces no question at all.

The ranking above decides *what* to ask. This decides *whether* to ask. Without it the
system fires on every session by construction, and a generic question about something the
user has done a thousand times is precisely the nagging that gets a plugin disabled in week
two. Firing every session is a promise of relevance the extractor cannot keep.

Expect many silent sessions. That is the system working.

### Dedup by merge

One concept across six sessions is one topic with six occurrences. Merging is what makes
recurrence visible, and recurrence drives both probe selection and verdict weight.

**Two merges, at different scopes.** The above is *cross-session* and remains unbuilt — it
needs the SQLite state that does not yet exist. A *within-session* concern merge does
exist: moments circling one concern in a single session collapse into one moment carrying
its turns and a `returns` count, so a session that raised the same point three ways
produces one candidate rather than three competing for a tiebreak.

Within-session, recurrence does **not** drive selection — intent outranks it
(`rank_key = (intent_rank, -returns, first_turn)`). Repetition inside one session more
often means the agent explained badly than that the concern ran deep. Across sessions,
recurrence driving selection stands as designed; that claim is about a user returning to a
concept on separate days, which is evidence of a different kind.

### Attribution

Capture is greedy and does not attempt to determine whether the user learned something or
merely watched the agent do it. **The questioning resolves this.** If the user only watched,
they fail — and "you have shipped three PRs using this pattern and cannot explain why it
works" is the single most valuable output the system can produce.

## Resurfacing

Three numbers in a config file:

```
hollow  → 3 days
partial → 7 days
solid   → 30 days
```

**Resurfacing is scheduled per topic, from the accumulated topic verdict — not per claim.**
A single hollow claim does not schedule its own return; it downgrades the topic, and the
topic's verdict sets the date. Probe selection then picks an unquestioned or previously hollow
claim when the topic comes back. Claims are what get graded; topics are what get scheduled.

A due topic wins topic selection at the next session end — it takes precedence over anything
the extractor found. It does not generate its own event; nothing fires outside a session.

FSRS remains a known hole, left open on purpose. If the crude rule visibly misfires, that
is the evidence justifying real scheduling.

## Stack

- Python, uv (matches the author's other projects)
- SQLite. Local file, no server.
- CLI + Claude Code plugin hook. No HTTP, no frontend.
- LLM: provider configurable via env; no provider-specific code outside one adapter module

### Model selection: the user's, by not choosing one

grask does not name a model. The hook shells out to the user's already-authenticated Claude
Code CLI without a `--model` flag, so every stage runs on whatever the developer has
currently selected.

This is a default, not a setting. Three reasons it beats the alternatives:

- **No second credential.** The user is already authenticated; the plugin inherits it. This
  is most of what "BYO key" was going to cost, removed by not asking.
- **No stale pin.** A model named in config is wrong the moment a better one ships. A model
  read from the hook payload assumes a field that may not be there.
- **Their quality bar is their choice.** A developer on the strongest model gets sharper
  probes; one who has downgraded for cost gets what they chose. grask does not get to make
  that call on their behalf.

Per-stage overrides (cheap model for triage and extraction) remain available as explicit
config, unset by default. See "Deferred: making this cheap".

Deleted from v1: FastAPI (no web surface to serve), Next 14 / React 18 / TypeScript / pnpm
(no portal).

## Failure modes

| Failure | Behavior |
|---|---|
| LLM call fails mid-grask | Save transcript, mark session incomplete. Never lose user answers. |
| Extraction returns garbage | User skips, or votes no on "was this worth asking?". The no-vote is the signal — attributed to the hypothesis and probe, so the failure is locatable. A high skip rate alone is ambiguous between a bad question and a busy developer. |
| Hook crashes | Silent. Log to file, never to the user's terminal. |
| Judge is a coward | Caught by calibration tests. See below. |
| User skips forever | Backs off after 3, then stays quiet. Correct behavior, not a failure to fix. |

## Testing

The core is a pure function — `(topic, answers) → verdict` — testable without session,
plugin, or terminal.

- **Rubric generation:** golden tests on known topics. A rubric for retry safety that omits
  idempotency is a visible bug.
- **Grounded-rubric inference:** fixture code whose purpose is known, asserting the rubric's
  recorded inference about it is correct. A rubric that misreads the code fails the user for
  the model's mistake, and is the worst outcome in the system.
- **Silence:** a boilerplate-only transcript must produce no question.
- **Judge calibration — highest value tests in the repo.** Fixture answers at three known
  levels: genuinely solid, confidently hollow (fluent nonsense — the dangerous case), and
  honest IDK. **The judge must fail the fluent nonsense.** If it passes fluent bullshit,
  the project is broken. Write this test first.
- **Single-probe calibration:** the above, on one answer rather than five. This is the v2
  bet and the most likely thing to break. If the judge cannot catch fluent nonsense on one
  probe, the default price rises to 2–3 questions before any other change is made.
- **Budget:** deep sessions terminate in ≤8 questions, always.
- **Extraction:** real transcripts, asserting cap holds and dedup merges.

## Build order

1. Core: hypothesis → rubric → probe → answer → judge → **was this worth asking?**
   Validates the bet: does interrogation find real holes?
2. Judge calibration tests, including single-probe. Written alongside, not after.
3. `SessionEnd` hook + extractor (cap 1).
4. Resurfacing.
5. History view — only if its absence is actually felt.

Rationale: the capture pipeline is the fun part and the part that doesn't matter. A perfect
extractor feeding a dull question is an uninstall; a rough extractor feeding a sharp one is
a product. The loop in step 1 is the smallest thing that can produce a yes-rate, and until
it exists no other stage can be evaluated at all.

**Correction — the build has drifted from this order.** Stage 0 and stage 1 triage are
built; the core is not. That is the front half of step 3 completed before step 1, and it is
why stage 1 has been refined twice against hand-judgement rather than against any downstream
signal. Correcting it does not mean discarding that work: the 28 keeps from the 69-session
corpus run become the input to step 1.

**Step 1 is fed from real triage output, not hand-typed topics.** The `grask <topic>` CLI
remains the test harness and entry point, but validating probe quality on topics typed by
hand would exercise the one path where the fatal failure — misreading code the developer
actually wrote — cannot occur. That would measure a question quality that does not transfer.

## Open questions

**Whether developers care — the top risk, and it is not an engineering one.** Claude wrote
it, tests passed, PR merged. Twenty seconds is cheap, but it is not free, and the benefit
lands months later if at all. Two independent reviews of `IDEA.md` ranked this above the
coward-judge problem; this design had it second. No test in this repo can settle it.

Two things follow. First, no claim this design cannot support ("prevents outages") may be
used to motivate it. Second, if the honest answer is "few developers care," the correct
response is a good tool for the people who do — who self-select by installing it — not a
wider net cast over people who don't. That is why the "watch everything" direction
(Cursor, VS Code, git, Slack, design docs) stays rejected: surveillance does not
manufacture caring, and the developer who won't answer one question won't install it.

**LLM cost.** *Whose key* is now settled — the user's own Claude Code session, inherited
rather than configured. See "Model selection". A hosted option would drag back a server,
accounts, and billing, and is rejected for that reason rather than left open.

What remains open is the amount. Every qualifying session end costs a full transcript read
on the user's own quota, spent after they have walked away, on a question they may never
answer. Triage keeps this off most sessions, but the cost is real and lands on someone who
did not opt into that specific spend. If it turns out to be noticeable, the deferred
optimizations get pulled forward. Does not block build order 1–3.

**Terminal-only ceiling.** grask reaches Claude Code users and no one else. Judged correct
rather than limiting — that is where the described problem lives — but it is a deliberate
market cap and should be revisited only with evidence.

## Rejected designs

**Pull-based portal (v1's core).** Reversed in v2. See "Why not pull-based". The portal,
queue screen, and weekly report were all destinations, and a destination is a decision the
developer never makes. v1 accepted this and set expectations accordingly ("the user will
not show up"); a product cannot.

**Streaks, XP, scores, notifications.** Still cut. They manufacture guilt to compensate for
a trigger that isn't free. The trigger is now free.

**Weekly report.** Its headline — "hollow and recurring" — was the product in one line, and
it lived on a page nobody opens. Recurrence now feeds probe selection directly, which
delivers the same signal without asking anyone to read anything.

**Mining git history / PR review comments.** Most code in the user's repos is
agent-authored, and maintainer reviews on agent-written PRs are already collected
elsewhere. A diagnostic
pointed at this produces a confident report card *for the agent*, and a learning path for
mistakes the user never made.

**Correction-mining as spine.** Capturing what the user accepted/rejected/corrected in
agent output. Genuinely novel, terminal-coupled, forward-looking only. Cut when the user
reframed toward topic-initiated questioning.

**FSRS in v1.** Earns its complexity over hundreds of cards and daily reviews; this system
has neither. Deferred until the crude rule demonstrably fails.

**Full 5–8 grask at every session end.** The version v1 correctly rejected. Priced for
someone who chose to be there. It is the opt-in path, not the default.

**Lesson authoring (LLM-generated lessons).** Easiest thing to build, least valuable thing
to have. Any chat window explains idempotency in fifteen seconds. The scarce thing is
knowing it's your problem, with receipts.

## Revision history

**v2.4 (2026-07-21)** — external review. Question quality replaces the coward judge as the
central risk, matching what `IDEA.md` has said since v2.1; the judge becomes the secondary
risk. **Hypothesis** named as a first-class object between moment and rubric — it was
already present twice under other names (`concern`, and the rubric's "recorded inference"),
and naming it makes the rubric derived rather than invented. Verdict format corrected to
claim scope; `Topic: … Verdict: hollow` banned as an overreach one probe cannot support.
The intent taxonomy is made subordinate to a stated invariant, which demotes `contradiction`
and `pushed_back` to drop candidates. **"Was this worth asking?" added as the north-star
metric**, replacing skip rate as the quality signal and hand-judging as the evaluation
instrument. Build order restated with a note that the repo has drifted from it.

**v2.3 (2026-07-20)** — stage 0 built and run against 107 real transcripts. Cost section
replaced with measurements: the transcript read the v2.2 design was optimizing against is
0.8 KB/session, and code is the only real input cost. Two extraction bugs found only by
running on real data — `suggestion_accepted` turns (18% of human input, question-heavy) were
being discarded, and subagent transcripts were excluded by glob accident rather than intent.

**v2.2 (2026-07-19)** — capture split into three stages (triage / seed / question+rubric)
running in one `SessionEnd` invocation, with seeds stored so probes are regenerable after a
prompt change. Model selection resolved: inherit the user's current Claude Code model by not
passing one, which also settles "whose key". Cost optimizations named and explicitly
deferred until a question hit rate exists to justify them.

**v2.1 (2026-07-17)** — external review of `IDEA.md`, two independent reviewers. Probes
become artifact-grounded (generated against the session's real code, not the concept), with
the rubric-inference risk and the "your premise is wrong" escape written in rather than
papered over. Silence added as a valid outcome — the system no longer fires on every
session. Motivation promoted to the top open question. "Watch everything / Developer OS"
evaluated and rejected. Examples moved off inheritance onto AI-assisted engineering.

**v2 (2026-07-17)** — audience changed from "one: the author" to developers using Claude
Code. Consequences: pull-based portal deleted along with the entire web stack; session end
became the trigger; default price dropped from 5–8 questions to 1, with 5–8 opt-in; verdict
scope narrowed to claim-level and accumulates over recurrence; hollow verdicts now point at
where to look. All four anti-cowardice controls carried over unchanged.

**v1 (2026-07-17)** — original. Single-user, pull-based, portal + weekly report.
