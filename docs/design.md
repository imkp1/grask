# grask — Design

How grask works and why each part is shaped that way. This is the current document: when
the code and this disagree, one of them is a bug.

## Premise

You cannot tell the difference between understanding something and having watched it
happen. Reading about idempotent retries and nodding along feels identical, from the
inside, to knowing it. Watching an agent apply a pattern across three PRs feels like
learning it.

grask finds out which one it was, by asking a question you can't bluff.

## Goals

- **Primary:** surface blind spots in how the user engineers — gaps they cannot self-report
  because they don't know they have them.
- **Secondary:** counter staleness. Concepts that keep recurring in real work and keep
  grading wrong are the highest-value output of the system.
- **Third:** be used without willpower. A developer must encounter grask because they
  coded, not because they remembered it existed.

## Users

Developers who write code with Claude Code. Not a single-user tool, and that is the
constraint everything else follows from: a tool one known user tolerates is not a tool
strangers install twice.

## Non-goals

Auth. Multi-user accounts. Content library. Lesson authoring. Streaks, scores, XP,
notifications. FSRS. Cursor capture. Mining git history or PR review comments. Web portal.
Weekly report. **A judge.** Free-text answers. A confidence rating.

Each was considered and cut. Rationale is in "Rejected designs".

## Interaction model

**Trigger-based, attached to work the developer already does.** grask captures at the end
of a Claude Code session — the one moment in the day when the topic is loaded in the
developer's head rather than only in a database — and delivers the question the next time
the developer runs `grask` or `/grask`.

The system never asks the developer to go anywhere. There is no destination.

Price of an encounter: **one multiple-choice question, ~20 seconds.** Pick a letter, read
two sentences, done. There is no follow-up, no second question, and no way to end up in a
longer session than the one advertised.

**The trigger is free and the price is 20 seconds.** That is the whole economic argument for
the product, and every mechanic in this design either protects it or is cut. It is why there
are no streaks (they buy a habit the trigger already gives), why the cap is one question
(the price is the promise), and why silence is a first-class outcome (a question with
nothing behind it spends the 20 seconds and returns nothing). When a proposed feature raises
either half, that is the reason to reject it.

Many sessions produce no question at all. See "Silence is a valid outcome".

**Capture and delivery are split: push to capture, pull to deliver.** The design once fired
the question at session end. It does not: the hook runs detached, the developer has already
walked away, and a prompt written into a closing terminal is a prompt nobody reads. Capture
happens when the evidence is freshest; the question waits, for at most seven days, until the
developer asks for it. So the half the developer would have to remember — noticing a session
was interesting — is automatic, and the half they choose is one command with a question
already waiting behind it. The split is also what makes the question cost nothing at the
moment it is generated: nobody is sitting there while three model calls run.

The original pull-based portal is a rejected design; see "Pull-based portal". The dismissal
risk that motivated it is real and is priced against structurally in "Restraint".

## Architecture

One package. No server. No runtime dependencies.

```
SessionEnd hook (grask-hook)
        │  payload on stdin, spawn detached, exit 0
        ▼
capture worker  (python -m grask.capture)
        │
   transcript.py ─0─▶ triage.py ─1─▶ select.py ─▶ seed.py ─2─▶ probe.py ─3─▶ SQLite
                                                                                │
                                              ┌─────────────────────────────────┘
                                              ▼
                                    storage.next_probe()
                                       │              │
                              cli.py (terminal)   cli.py serve/record
                                       │              │
                                    ask.py         SKILL.md → /grask
```

Numbers are the stages named in "Capture". Everything left of SQLite spends money;
everything right of it does not.

### The capture worker

`hook.py` reads the `SessionEnd` payload, spawns `python -m grask.capture` with
`start_new_session=True`, and returns 0. The parent is gone long before the first model
call, so grask is non-blocking by construction rather than by the harness's permission.
Both output streams go to `~/.claude/grask/grask.log`; a detached process writing to an
inherited terminal is a process that scribbles on the next thing the developer does.

`capture.py` never raises. Nothing is watching its exit code, so every failure has to
become a row and a log line instead.

### Delivery

`ask.py` is pure logic with an injected console: `(PendingProbe, Console) → Interrogation`.
It has never heard of a TTY. Two surfaces drive it:

- **`cli.py` terminal path** — `TerminalConsole`, `print` and `input`.
- **`grask serve --json` / `grask record`** — the non-interactive seam. `SKILL.md` (the
  `/grask` skill) calls `serve`, renders the question through Claude Code's native question
  UI, and calls `record` with the letter. `serve` prints the question blind: no answer key,
  no explanation, so the model rendering the UI cannot leak the answer.

`serve` consumes nothing. An abandoned Claude session leaves the probe pending, which
matches Ctrl-C in the terminal path.

### State

One SQLite file at `~/.claude/grask/grask.db` (`GRASK_HOME` relocates it). Five tables:

| Table | Holds |
|---|---|
| `sessions` | one row per session seen, whatever the outcome — `ask` \| `silent` \| `error` |
| `seeds` | stage 2's topic, verified quotes, refs, decision, hypothesis |
| `probes` | the question, shuffled options, `correct_idx`, explanation |
| `asks` | one row per probe answered, `UNIQUE(probe_id)` |
| `answers` | the option text the developer picked |

**Silence and failure are recorded, not just keeps.** Keep-rate and failure-rate are the two
numbers that say whether any of this works, and a table you have to remember to populate is
a table that lies. It is also what makes capture idempotent: a `session_id` already present
means we have seen it, whatever we concluded.

`UNIQUE(probe_id)` on `asks` makes an answer permanent. That is why Ctrl-C records nothing
rather than recording a skip — a stray keypress must not consume the question.

### Entry points

1. **`grask`** — ask the next pending question in the terminal. The product.
2. **`grask serve --json` / `grask record <id>`** — the machine-readable pair behind
   `/grask`. Also the test harness for delivery.
3. **`grask skill [--install] [--dir]`** — write the `/grask` skill into a skills directory.
4. **`grask-hook`** — the `SessionEnd` capture trigger. Registered in the user's
   `settings.json`; never invoked by hand.

There is no `grask <topic>` entry point. An earlier build order had one as a way to exercise
the questioning core without a capture pipeline attached; it was never built, because a
hand-typed topic is the one path where the fatal failure — misreading code the developer
actually wrote — cannot occur, so a probe validated that way would measure a quality that
does not transfer. The corpus runner (`grask.capture_run`) replaced it: it exercises the
same core against real transcripts.

## The questioning engine

The project succeeds or fails here.

### Central risk: the question might not be worth asking

**The question is the product. Everything else is support.** A bad question is an uninstall:
*"what is a retry policy"* tells the developer instantly that this thing has nothing to
teach them, and no downstream quality recovers that.

So the bar is not "a relevant question." It is a question that makes an experienced
engineer stop for thirty seconds and go *"…huh"* — every session, from their own code.
Whether an LLM clears that bar reliably is the bet this project is making. It is measured,
not argued: see "North-star metric".

This and developer motivation are the two risks that outrank everything below; see "Open
questions" for the second and for how they rank against each other.

**The question must teach something portable.** A question whose answer is "because this
file says so" — the contents of a local script, the wording of a local spec, what one step
of a local design does — is answerable only by whoever sat through the session, and is worth
nothing the moment they close the file. The artifact names the setting; the mechanism must
outlive it. The test, applied before writing: *would a competent developer who never saw
this session be better at their work for knowing the answer?* If not, there is no probe
here, however specific the detail. This is stated in both the stage-2 and stage-3 prompts
because it is the failure that survives every structural gate — a recall question is
perfectly well-formed.

### The answer is a pick, not an explanation

The original design took a free-text explanation and had a second model call grade it. Two
opposite failures made that the riskiest part of the system:

- **The coward.** An LLM asked to grade an explanation says it's great. It accepts vagueness,
  fills gaps on the user's behalf, and calls it correct — manufacturing the exact false
  confidence the system exists to destroy.
- **The zealot.** Worse, and underrated at first: the judge misreads the code, invents a bug
  the developer didn't write, and tells them confidently they don't understand something they
  do. A confident false accusation about your own code, in front of you, gets the plugin
  disabled forever, and it should.

The design accumulated four structural mitigations for the coward (separate asker and judge,
frozen rubric, mandatory quoting, code-grounded probes) and three for the zealot. All of them
were controls on an LLM's judgement at answer time.

**Multiple choice deletes the judge instead.** The answer key is minted at generation time,
before the developer exists to the question. Picking an option *is* the answer: the verdict
is `pick == correct_idx`, decided in `ask.grade`. There is no model call anywhere in the ask
path. A judge cannot be slow, expensive, cowardly, or zealous if there is no judge.

What that buys, in order of importance:

- **No verdict a model can argue with.** The zealot fails closed: the worst a wrong key can
  do is mark one pick incorrectly, against a stated explanation the developer can read and
  reject. It cannot compose a paragraph about why their code is wrong.
- **The 20 seconds is real.** Free text meant typing, waiting for a grader, and reading a
  verdict. A letter is one keypress.
- **`/wrong` becomes cheap.** With no judge to negotiate with, rejecting the premise is just
  another outcome, recorded as `premise_rejected`.

**What it costs, stated plainly.** Recognition is easier than recall — a developer who could
not have explained the mechanism may still eliminate three wrong options. The design accepts
this and pushes the burden onto the distractors: every wrong option must describe a
*plausible wrong mechanism*, something a developer who half-understood the decision would
actually believe. The dangerous failure is a fluent answer describing a different mechanism,
and the distractors are the only place left to catch it. An option that is a joke or an
obvious throwaway converts the probe into a free pass, which is why the stage-3 prompt bans
them and the option gates reject duplicates.

Each option asserts exactly one mechanism. An option coupling two claims with "and" — a
limit *and* a transformation, a rule *and* its consequence — is unusable even as the correct
one: whoever picks it cannot tell which half was graded, and the unchecked half is where a
falsehood survives.

### The Hypothesis

Between the moment and the question sits a **falsifiable claim about what the developer
accepted without understanding.** Not a topic label — a sentence that can be wrong:

```
moment      "why do we need the idempotency key here?"  (turn 14)
topic       idempotency keys
hypothesis  The developer accepted that a key prevents double charges without
            knowing the key must be stable across retries to do so.
```

It is stage 2's most important output, stored on the seed and carried onto the `Rubric` the
probe is minted with. Naming it does three things:

- **The question becomes derived rather than invented.** Stage 3 is not asked "write a
  question about idempotency"; it is asked to test the mechanism at the core of a specific
  claim. Generating a question from a bare topic name is why "question quality" reads as an
  unsolvable ceiling; generating it from a stated claim gives it something to be accountable
  to.
- **"Your premise is wrong" gets a target.** `/wrong` is a clean refutation of a specific
  claim, which is what makes it loggable as a bug report against grounding rather than a
  vague right to complain.
- **A failure is diagnosable.** A wrong pick, or a `premise_rejected`, is attributable:
  wrong hypothesis (triage found nothing real), or right hypothesis tested by a bad question
  (stage 3 asked it poorly). Those need opposite fixes. Without the hypothesis object a bad
  probe is an unactionable complaint.

`seed.py` rejects a hypothesis under 8 words, because the observed failure is the model
restating the topic as a noun phrase, and a noun phrase cannot be wrong. It also rejects
hedges — "may not fully understand the implications" is unfalsifiable, so no answer can ever
settle it — though that one is prompted for rather than enforced.

**The hypothesis is internal. It is never shown as the framing of the question.** "You
copied this without understanding idempotency" stated to a developer's face is precisely the
confident accusation this design refuses to make. The system asks; it does not accuse. The
hypothesis drives the question, is stored, and never becomes the greeting.

It is also the riskiest object here, because it is the hallucinated premise from "Known
ceiling" given a name. Naming it does not reduce that risk — it makes it inspectable.

### Loop

```
transcript ──▶ moments ──▶ selected moment ──▶ hypothesis ──▶ probe + answer key
                                                                       │
                                                                    stored
                                                                       │
                                                            (later, on demand)
                                                                       │
                                                                       ▼
                                        question + 3-4 options ──▶ pick | enter | /wrong
                                                                       │
                                                          pick == correct_idx
                                                                       │
                                                                       ▼
                                                            ✓ / ✗ + explanation
```

Everything above `stored` runs detached with nobody watching. Everything below runs with no
model call at all.

### What one probe can and cannot say

**One question can identify one misconception. It cannot identify understanding.** These are
not the same thing, and the distinction has to survive into the user-visible strings, not
just the prose here. A correct pick means one option was recognised as correct. It does not
mean the developer understands retries, and nothing grask prints may say it does.

This is why there is no grade, no score, and no per-topic verdict. The stored outcomes are
flat:

| Outcome | Means |
|---|---|
| `passed` | the pick matched the key |
| `failed` | it did not |
| `skipped` | enter, Ctrl-D, or `--skip` |
| `premise_rejected` | `/wrong` — the question misreads what happened |
| `error` | the stored row is too malformed to grade honestly |

`premise_rejected` is its own outcome rather than a flavour of skip because it is the zealot
rate, and a rate you cannot query is a rate nobody checks. `error` exists because grading a
row with a broken option list would invent a verdict, which is worse than admitting the row
is broken.

What one probe cannot say, accumulation could. That is unbuilt; see "Dedup".

### Structural gates

Instruction is not a control. Four rules are stated in prompts *and* enforced in code,
because each one was observed to be ignored:

**The evidence rule (stage 1, `triage.parse_moments`).** Every moment must quote the
developer verbatim and name the turn the quote came from. The quote must appear in *that
turn*, not merely somewhere in the session — that is what makes the turn index trustworthy
as the moment's identity. An `asked_why` whose quote asks nothing is rejected. Rejections are
per-moment, not per-session: one bad moment in a list of six is a bad moment, not a failed
session. A session where every moment was demoted is recorded with `demoted_from_ask`, which
is a bug report against the prompt rather than against the developer.

**The quote rule (stage 2, `seed.verified_quotes`).** A claimed quote that appears in nothing
the developer typed is discarded; a seed with no surviving quote is rejected outright.
Comparison collapses whitespace, because verification must not be so literal that a
re-wrapped genuine quote fails — failing true quotes would push the design toward trusting
the model instead, which is the wrong direction to be pushed.

**The one-question rule (stage 3, `probe.reject_if_compound`).** A stem with two questions
cannot have one correct option, so this gate is what keeps the mechanical verdict meaningful.
Counting question marks catches `"…? And how…?"`. It misses the shape stage 3 actually
produces, observed on the first real run — *"what has to be true of that payload …, and how
would you find out if it stopped being true?"*: one mark, two questions, about a minute of
work. So a second pattern catches a conjunction after a comma. Requiring the comma keeps
ordinary subordinate clauses out of the net at the cost of missing a comma-less second
question, which is the right direction to be wrong: a false reject costs one regenerated
probe, a false accept costs the developer their twenty-second promise.

**The option gates (stage 3, `probe.validate_choices`).** 3–4 options, no duplicates, a
`correct` index that names a real option, a non-empty explanation. Four is the ceiling
because Claude Code's native question UI takes no more; rows over the cap stay pending for
the terminal path rather than being consumed.

All four are structural rather than qualitative. Whether a question is *good* is settled by
the yes-rate, not by a gate; whether it is one mechanically gradable multiple-choice question
is settled here, because a no on that makes the yes-rate uninterpretable.

**Rejection retries, up to three attempts.** The failure is stochastic — the same seed
produced two different compound questions across two real runs — so resampling is the right
response. A rejection goes back with the offending question quoted and the reason named,
because the rule it broke was already in the prompt and was already ignored; a plain call
failure goes back unchanged, because nothing about the prompt caused it. Every attempt is
billed, so `cost_usd` sums all of them: a cost that counts only the winning call makes
stage 3 look cheaper than it is.

**Options are shuffled at storage time, not display time.** The stored row is the single
source of truth for what position was shown, so `correct_idx` is minted post-shuffle, once.

### Skipping is free. So is rejecting the premise.

Pressing enter without answering is always valid and carries no penalty. Ctrl-D reads as the
same deliberate "not now". A skip that costs something produces a bluff or an uninstall.

`/wrong` ends the probe with no penalty and prompts once for an optional reason. Optional,
because requiring an argument to escape is how you get an escape hatch nobody uses — the
outcome is the signal, the text is a bonus.

This is a hard design constraint. The system depends on the developer being willing to be
wrong in front of it.

### Payoff: the explanation

The explanation is written at generation time and shown after the pick, right or wrong. It
states the mechanism in one to three sentences and stops.

It must not extend into a downstream consequence: the clause after "so", "which means", or
"that's why" is where stage 3 is wrong most often — a true mechanism carries an invented
result, and the developer who answered *correctly* still leaves with the falsehood. If the
consequence is worth testing, it belongs in the options as a distractor, not asserted as
fact after the pick.

This is aiming, not teaching. Lesson authoring stays cut: *"any chat window explains
idempotency in fifteen seconds. The scarce thing is knowing it's your problem, with
receipts."* The payoff is knowing which fifteen seconds are yours.

### What the developer sees

```
from 2026-07-21 · retry backoff in the webhook dispatcher

Your retry loop sleeps 2**attempt seconds between attempts. Why does adding random
jitter matter more as the number of clients grows?

  a) Jitter reduces the total number of retries each client makes.
  b) Clients knocked out together retry together; jitter spreads them back out.
  c) Exponential backoff overflows without a random term to bound it.
  d) Jitter is what makes the sleep interruptible by a signal.

pick   [a-d]   ·   enter = skip   ·   /wrong
> b
✓ Backoff decides how long each client waits. It does nothing about them all waiting
the same amount. Clients dropped by one outage come back in lockstep, so the recovering
service takes the same thundering herd on every cycle. Jitter decorrelates the schedules.
```

The context line is one line and mandatory. Without it the developer reads a question about
work they cannot place, which is the version of this tool that feels like a quiz.

**No topic ever takes an adjective.** The topic is a heading and, eventually, a scheduling
key. A string like `retries: hollow` asserts something one probe cannot support, and it is
banned. It is easy to be careful about this in prose and sloppy in the output format, which
is the only place the developer actually reads.

### Known ceiling

**Answer-key quality caps everything.** A wrong key marks a correct pick incorrect, with a
confident explanation, and there is no judge left to blame. Mechanical grading did not remove
this ceiling — it moved it earlier, from answer time to generation time, where at least it is
inspectable and re-runnable.

**Grounding in real code raises the stakes.** A question about a concept is wrong only if the
model is ignorant. A question about *this interface in your codebase* requires the model to
have correctly inferred why the interface exists, and when it infers wrong, the developer is
marked wrong against a premise that is simply mistaken.

Mitigations:

- **The hypothesis is stored**, so when a question feels off, it is the first thing to read.
- **A high `premise_rejected` rate is a bug report against grounding.** `/wrong` makes the
  zealot measurable instead of merely survivable.
- **Seeds are stored and re-runnable.** When the stage-3 prompt improves, every past seed can
  be re-run into a better probe without needing transcripts back.

## Restraint

The case against session-end prompts is right about how push-based tools die. Four
structural limits, none of them tonal:

**One question, ever, per invocation.** `next_probe` returns one row. There is no queue
screen, no "next question", no way to turn an encounter into a session.

**Skipping is free.** See above.

**Questions expire after 7 days.** A probe about work you did last week is a quiz. Expiry is
computed at query time rather than stored, so nothing has to sweep and no lifecycle column
can fall out of sync with the clock. This is also the backlog control: grask cannot
accumulate a debt of forty unanswered questions, because it silently forgets the old ones.

**Newest first.** `next_probe` orders by `created_at DESC`. Oldest-first would lead with the
session you have most thoroughly forgotten, which is the quiz failure again.

**Not built: three consecutive skips → silent for a week.** The design's only retention
mechanic, and it works by backing off. Nothing in the code tracks a skip streak yet. It
matters less than it did when the question fired at you unprompted — today, not running
`grask` already achieves it — but it is still the right behaviour for the `/grask` surface,
where a skip is a signal the developer showed up and found nothing worth their time.

## North-star metric

**Not built.** This is the largest gap between this document and the code, and it is
recorded here rather than quietly dropped, because everything else in the design is
supposed to be evaluated against it.

The intent: after the explanation — so it costs nothing in the moment it would distort —
grask asks one thing.

```
was this worth asking?   [ y / n ]
```

**Yes-rate would be the north-star metric.** If grask consistently earns a yes, almost
everything else becomes tractable; if it doesn't, no amount of taxonomy work or capture
engineering matters. It is the direct measurement of the central risk named above, which
still has no instrument.

**It would replace skip rate as the quality signal.** Skip rate conflates *bad question* with
*busy developer* — two things needing opposite responses, which it cannot distinguish. A
binary asked after the answer separates them. A no-vote would also be diagnosable the way a
wrong pick already is, against the recorded hypothesis.

Constraints, so the metric does not violate the design that produced it: one keypress,
skippable like everything else, never framed as feedback *on the developer*, and it never
asks why. A follow-up "what was wrong with it?" is the same nagging this design rejects
everywhere else.

**What exists in the meantime** are two proxies, both weaker: the `premise_rejected` rate
(the question misread the session) and the `skipped` rate (ambiguous, as above). Until the
vote exists, question quality is judged by the author reading probes.

## Capture

Claude Code `SessionEnd` hook. Reads the transcript, runs four stages cheapest-first, writes
what survives, exits. Fails silently.

### Four stages, one invocation

Each stage filters, so only what survives pays for the next.

**Stage 0 — extract (`transcript.py`, free).** Pull the developer's own turns out of the
session log. Tool results, file snapshots, and injected skill text are not the developer
thinking. A session with no human turns is recorded `silent` without a single model call.
41% of sessions stop here.

**Stage 1 — triage (`triage.py`, one call).** Is there anything here worth asking about?
Lists *every* qualifying moment, each anchored to a verbatim quote and the turn it came
from. It does not choose between them. Sees the developer's turns and the *paths* of files
touched — never file contents: deciding *whether* a session has an engaged-with concept is
answerable from ~1.3KB of what the developer typed, and code is the expensive input. Most
sessions yield nothing, and an empty list is the correct answer.

**Select (`select.py`, free).** Rank the moments and pick one. Deliberately code, not
prompt. Measured over 6 sessions × 3 runs, the model finds substantially the same moments
every time — 26 of 29 keep their signal, and topic wording is stable — but a session carries
2–9 qualifying moments and a single call picks among them arbitrarily. That arbitrary pick
was the whole of the observed topic instability.

**Stage 2 — seed (`seed.py`, one call).** State, as a falsifiable claim, what the developer
may have accepted without understanding, plus the topic, the verified quotes, the `file:line`
refs, and the decision that shipped. Stored and re-runnable; see "Known ceiling".

**Stage 3 — probe (`probe.py`, one call).** Write one multiple-choice question about the
mechanism, with the answer key and the explanation. Reads the full dialogue — turns, agent
replies, and the before/after text of edits — not just the seed.

**Why stage 3 reads the transcript and not the seed.** A compressed seed is enough to name
the topic; it is not enough to name the file, flag, or identifier that actually shipped, and
a question that cannot do that is a generic question. At 0.8 KB of human input per session
there is no cost side to this tradeoff. See "Measured cost".

**Why one invocation.** The transcript is the fragile input: transcript files rotate, and the
diff a seed references drifts as the branch moves. Reading it once, at the moment it is
freshest, is worth more than the work saved by deferring.

**Why stage 3 is not lazy.** Deferring question generation to `grask` invocation would only
produce questions for sessions someone chose to open — a biased sample of the one thing most
in need of unbiased measurement. Rejected on those grounds, not on cost.

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

- **Stage 0 is free**, and it alone decides 41% of sessions produce nothing. The "heuristic
  triage to save money" optimization is already built — it is stage 0 — and stage 1 only ever
  sees sessions with real human input.
- **Transcript size is not a cost driver.** 0.8 KB of prompts per session cannot justify a
  cheaper model for any stage.
- **Code is the only real input cost**, and only on the 40% of sessions that touch files.

**End to end, measured across the corpus runner's own batches: $0.27 per session selected,
with a 40% band both ways, and a 42% keep rate** (25 kept of 60 selected, $16.36). The
estimate is deliberately one all-in number per session rather than a triage/seed/probe
decomposition. The decomposition looks more principled and fits worse: two observed batches
($1.62/10 and $14.74/50) cannot be reconciled by any single set of per-stage constants,
because session length varies more than stage price does. A model that cannot fit the two
points it was built from should not be dressed up as three constants.

An earlier version of that estimate was fitted to n=14 from a different population — the
hook's grask-only sessions — and under-quoted a 50-session run by 64%. Erring low is the
harmful direction, because it under-quotes spend the developer then authorises. Quote the
band, not the point.

The one optimization still worth having is **diff rather than whole files**. The figures
above count entire current files, which is the naive strategy; the session's actual diff is a
fraction of that and is what a grounded probe needs anyway. Deferred until question quality
is known, because a probe may well need surrounding context that a diff omits.

Two smaller ones are already in `llm.py` and were worth more than they look. Every call
inherits the user's Claude Code context, and the skill listing is the largest part of it —
measured at 20.5k → 7.8k tokens ($0.146 → $0.078) on a config with 1822 installed skills, by
passing `--disable-slash-commands`. Tools are disallowed for the same reason: grask sends one
self-contained prompt and wants one JSON object back, and granting tools would let a stage
wander into the repo, turning a fast classifier into an agent. `--bare` would cut more and is
rejected — it reads auth strictly from `ANTHROPIC_API_KEY`, which breaks the "no second
credential" property the model-selection section is built on.

### What counts as a topic

The governing question, from which everything else follows:

> **What evidence suggests the developer accepted something without fully understanding it?**

The taxonomy is a set of detectors for that invariant and is subordinate to it. If a signal
stops serving the invariant, the signal goes — it does not get to redefine the principle by
being the thing that happens to be implemented.

Four signals, defined in `triage.py` and ranked in `select.py`. The split that matters is
**whether a quote can prove the signal at all**:

| Rank | Signal | Evidence | What it is |
|---|---|---|---|
| 0 | `asked_why` | quote-provable | They asked why. Their curiosity, not the agent's output. |
| 1 | `pushed_back` | quote-provable | They corrected, overrode, or disagreed. Judgment showing. |
| 2 | `new_pattern` | code-grounded | A pattern, library, or technique newly landed in their code. |
| 3 | `explained_at_length` | code-grounded | The agent explained at length and they took it on board. |

For the first two, the developer's own words *are* the evidence — a why-question or a
correction is visible in the quote itself. For the second two the quote can only ever be
circumstantial: a pattern landing in the codebase is shown by the code, not by anything the
developer typed. Those are kept but flagged `weak_evidence`, and stage 2 has to ground them
in the dialogue before they earn a question.

**The ranking is derived, not asserted:** signals whose quote is self-proving come first,
because preferring the others would make selection favour the weakest evidence available.
The shorthand is **quiz what they were told, not what they told the agent** — a question they
asked means an answer they received that nobody checked.

`rank_key` is `(signal_rank, -turn)`: signal first, then the latest turn, because with signal
equal the more recent engagement is the one still fresh when the question arrives. It depends
only on the moment itself, never on the other candidates, which is what keeps the winner
stable when extraction adds or drops a marginal moment between runs — and it does.

**Prefer mechanisms over process.** Both stage 1 and stage 2 down-rank behavioural moments —
why a message was phrased a certain way, workflow or etiquette choices, why the agent took
the approach it took. A mechanism has a right answer a question can test; a process choice
mostly does not.

Explicitly not qualifying: files edited, commands run, tests made to pass, config changes,
dependency bumps, renames, lint fixes, a bug the developer diagnosed themselves, writing
prose or commit messages, or a session where the developer only said "continue" and "fix it".
That is activity, and activity is not learning.

**A proposed re-cut, unbuilt.** An intent-shaped taxonomy — `definition_gap` ("what is X"),
`asks_rationale` ("why X"), `counter_proposal` ("why not Y"), `asserts_belief` ("…right?") —
splits the current `asked_why` into four and orders them by strength of acceptance evidence.
It is a finer instrument for the same invariant and is not implemented; `triage.py` and
`select.py` are the current taxonomy. It should not be built before the yes-rate exists,
because there would be nothing to evaluate the change against.

If `definition_gap` is ever built, it needs a guard: ranking it first names the *moment*,
never the question. The question must still be grounded in the code the developer shipped —
"your ranker does X, what happens when Y", never "define single-axis queries". A bare
definitional question is the failure `IDEA.md` calls unrecoverable.

### Cap: 1 topic per session

One, not the two or three an earlier draft allowed. A topic is used now, not stored against a
backlog, so there is nothing to fill. Missing topics is acceptable — they recur if they
matter.

Stage 1 still returns every qualifying moment, and the whole list is available on the
verdict. Only one becomes a seed.

### Silence is a valid outcome

**If no moment clears the bar, grask says nothing.** A session of boilerplate, config edits,
dependency bumps, or a bug the developer drove themselves produces no question at all.

The ranking above decides *what* to ask. This decides *whether* to ask. Without it the system
fires on every session by construction, which is a promise of relevance the extractor cannot
keep — and a generic question is the week-two uninstall the whole design is priced against.

Silence is recorded as a `sessions` row with verdict `silent`, distinguished from `error`.
Expect most sessions to be silent. That is the system working.

### Dedup — not built

One concept across six sessions should be one topic with six occurrences. Merging is what
would make the recurrence in "Goals" visible at all. Neither scope exists today:

- **Cross-session** merge needs a topic identity that survives rewording, and topics are
  currently free text on the seed. Two sessions can produce near-identical probes and nothing
  notices.
- **Within-session** merge — collapsing moments that circle one concern into one candidate
  carrying a `returns` count — was designed and is not implemented. `rank_key` has no
  `returns` term. In practice the cap of one plus a deterministic rank already picks a single
  moment, so the missing merge costs nothing today beyond the count.

If within-session merge is built, recurrence must **not** drive selection: repetition inside
one session more often means the agent explained badly than that the concern ran deep.
Across sessions, recurrence driving selection stands as designed — a developer returning to a
concept on separate days is evidence of a different kind.

### Attribution

Capture is greedy and does not attempt to determine whether the developer learned something
or merely watched the agent do it. **The question resolves this.** If they only watched, they
pick wrong — and "you have shipped three PRs using this pattern and cannot explain why it
works" is the single most valuable output the system can produce. It is also the output that
needs cross-session dedup, which is why that gap is the one worth closing next.

## Resurfacing — not built

The intent: a probe that graded wrong comes back, because the point is not catching a gap
once but finding out whether it filled in.

```
failed            → 3 days
premise_rejected  → not at all; the question was wrong, not the developer
passed            → 30 days
```

Two things have to exist first, and neither does: a **topic identity** stable enough to
schedule against (see "Dedup"), and a **second question on the same topic**, since resurfacing
the identical probe with the same four options measures memory of the options. That second
question is cheap — the seed is stored and re-runnable through stage 3 — which is most of why
seeds are stored at all.

Scheduling is per topic, never per probe. Probes are what get graded; topics are what get
scheduled.

FSRS remains a known hole, left open on purpose. If a crude three-number rule visibly
misfires, that is the evidence justifying real scheduling.

This is the largest unbuilt piece of the product, not a nice-to-have: `IDEA.md` calls the
second visit "the half that matters most".

## Stack

- Python 3.12+, uv. No runtime dependencies.
- SQLite. Local file, no server.
- CLI + a `SessionEnd` hook + a Claude Code skill. No HTTP, no frontend, no build step.
- LLM: the user's own Claude Code CLI, shelled out to. `llm.py` is the only module that
  knows a subprocess is involved.

### Model selection: the user's, by not choosing one

grask names no model. Every stage runs `claude -p` with **no `--model` flag**, so it runs on
whatever the developer currently has selected.

This is a default, not a setting. Three reasons it beats the alternatives:

- **No second credential.** The developer is already authenticated; grask inherits it. This
  is most of what "BYO key" was going to cost, removed by not asking.
- **No stale pin.** A model named in config is wrong the moment a better one ships. A model
  read from the hook payload assumes a field that may not be there.
- **Their quality bar is their choice.** A developer on the strongest model gets sharper
  questions; one who has downgraded for cost gets what they chose. grask does not get to make
  that call on their behalf.

There is no provider abstraction and no `--model` override, per-stage or otherwise. An
earlier draft reserved both; measurement argued against ever using them (see "Measured
cost"), and an adapter with one implementation is a layer, not a design.

## Failure modes

| Failure | Behavior |
|---|---|
| Hook gets a payload it cannot parse | Log it, return 0. Never speaks. |
| Any stage raises during capture | Log the traceback, record the session `error`, exit 0. `capture_session` does not raise. |
| Triage's model call fails | Recorded `error`, not `silent`. |
| Stage 3 returns an unusable probe | Retried up to 3 times with the rejection quoted back; then the session is written off. |
| Stored probe row is malformed | Served as `error` and consumed, so it stops blocking the queue. |
| Question misreads the session | `/wrong` → `premise_rejected`, with an optional reason. |
| Ctrl-C mid-question | Records nothing. The probe stays pending. |
| Developer skips forever | Nothing happens. The 3-skip backoff is unbuilt; see "Restraint". |

## Testing

223 tests, no network, no model. The design that makes this possible is the injected
console in `ask.py` and the injected stages in `capture_session` — every path that would
call a model takes the callable as an argument. One further test, marked `calibration`, runs
the real pipeline against a real model and is deselected by default because it costs money.

- **Structural gates** (`test_probe.py`) — compound questions, duplicate options, an out-of-
  range `correct`, a missing explanation. Each gate has the observed failure as a fixture,
  not an invented one.
- **The evidence rule** (`test_triage.py`) — a quote in the wrong turn, an `asked_why` that
  asks nothing, an unknown signal. Rejections are per-moment.
- **Quote verification** (`test_seed.py`) — a fabricated quote is dropped; a re-wrapped
  genuine one survives; a seed with no surviving quote is refused.
- **Selection stability** (`test_select.py`) — the winner does not move when a marginal
  moment is added or dropped.
- **Grading** (`test_ask.py`) — pick, skip, `/wrong`, and the malformed-row `error` path,
  driven by a scripted console.
- **Both delivery surfaces** (`test_cli.py`, `test_serve_record.py`) — including that `serve`
  never emits the key or the explanation, and that a double `record` is refused rather than
  overwriting.
- **Silence** (`test_capture.py`) — a session with no human turns is recorded silent without
  a model call; a triage error is recorded `error`, not `silent`.
- **Storage** (`test_storage.py`) — TTL, newest-first, the option cap, and migration of a
  database created before multiple choice.

**Not testable here, and named so it is not mistaken for covered:** whether the questions are
any good. That is the north-star metric, and it needs the vote.

## State of the build

Working end to end: capture (all four stages), storage, both delivery surfaces, mechanical
grading, the corpus runner.

Not built, in the order they matter:

1. **The "was this worth asking?" vote.** Without it, nothing else can be evaluated.
2. **Cross-session dedup.** Two sessions can produce near-identical probes.
3. **Resurfacing.** The half `IDEA.md` says matters most.
4. **Three-skip backoff.**
5. **Diff rather than whole files**, if cost ever becomes noticeable.

The order is deliberate and was arrived at the hard way. The capture pipeline is the fun part
and the part that doesn't matter: a perfect extractor feeding a dull question is an
uninstall; a rough extractor feeding a sharp one is a product. Stage 1 has now been refined
twice against hand-judgement rather than against any downstream signal, which is the cost of
building the measurable half after the interesting one. The vote is what stops that
happening a third time.

## Open questions

**Whether developers care — the top risk, and it is not an engineering one.** Claude wrote
it, tests passed, PR merged. Twenty seconds is cheap, but it is not free, and the benefit
lands months later if at all. Two independent reviews of `IDEA.md` ranked this above the
judge problem; this design had it second. No test in this repo can settle it.

Two things follow. First, no claim this design cannot support ("prevents outages") may be
used to motivate it. Second, if the honest answer is "few developers care," the correct
response is a good tool for the people who do — who self-select by installing it — not a
wider net cast over people who don't. That is why the "watch everything" direction (Cursor,
VS Code, git, Slack, design docs) stays rejected: surveillance does not manufacture caring,
and the developer who won't answer one question won't install it.

**Whether recognition is enough.** Multiple choice removed the judge and with it both of its
failure modes, but a pick is weaker evidence than an explanation. If passes turn out to be
cheap — developers eliminating three options without understanding the mechanism — the fix is
better distractors, not the judge's return. If better distractors are not enough, this
question reopens for real.

**LLM cost.** *Whose key* is settled — the user's own Claude Code session, inherited rather
than configured. A hosted option would drag back a server, accounts, and billing, and is
rejected for that reason rather than left open.

What remains open is the amount. A qualifying session costs three model calls on the
developer's own quota, spent after they have walked away, on a question they may never
answer. Stage 0 and triage keep this off most sessions, and the measured all-in figure is
$0.27 per session *selected* — but it lands on someone who did not opt into that specific
spend. If it becomes noticeable, the deferred optimizations get pulled forward.

**Terminal-only ceiling.** grask reaches Claude Code users and no one else. Judged correct
rather than limiting — that is where the described problem lives — but it is a deliberate
market cap and should be revisited only with evidence.

## Rejected designs

**A judge.** Free-text answers graded by a second model call. Designed in full, then cut:
every mitigation it accumulated was a control on an LLM's judgement at answer time, and
deleting the judgement was cheaper and safer than controlling it. See "The answer is a pick,
not an explanation".

**The confidence tap.** `how sure are you? [95% · 70% · 40%]` before answering, so a gap
could be measured against the developer's own number. Its payoff was never in the moment —
it needed the second visit to mean anything, and the second visit is unbuilt. Against a
four-option pick it also asks for a second keypress on a twenty-second promise. The `asks`
table keeps a nullable `confidence` column so historical rows keep their numbers.

**Pull-based portal.** The original shape of this project: a portal the user opened on
purpose, which rejected session-end prompts because *"a prompt at the end of every session
gets reflexively dismissed by week two, and then the plugin gets disabled."* Right about the
failure mode, wrong about the alternative — the same document also concluded the user would
not open the portal (*"The user will not show up daily. This is a stated constraint, not a
hope"*), so the real comparison was flawed-prompt vs. nothing, and it chose nothing. It also
assumed a session-end grask costs 5–8 questions; it costs one. The portal, queue screen, and
weekly report were all destinations, and a destination is a decision the developer never
makes.

**Streaks, XP, scores, notifications.** Still cut. They manufacture guilt to compensate for a
trigger that isn't free. The trigger is now free.

**Weekly report.** Its headline — "hollow and recurring" — was the product in one line, and
it lived on a page nobody opens. Recurrence should feed question selection directly, which
delivers the same signal without asking anyone to read anything.

**Mining git history / PR review comments.** Most code in the developer's repos is
agent-authored, and maintainer reviews on agent-written PRs are already collected elsewhere.
A diagnostic pointed at this produces a confident report card *for the agent*, and a learning
path for mistakes the developer never made.

**Correction-mining as spine.** Capturing what the developer accepted/rejected/corrected in
agent output. Genuinely novel, terminal-coupled, forward-looking only. Cut when the project
reframed toward topic-initiated questioning.

**FSRS.** Earns its complexity over hundreds of cards and daily reviews; this system has
neither. Deferred until a crude rule demonstrably fails.

**A 5–8 question deep session.** Correct as an opt-in path in a design with a judge and free
text; meaningless with a pre-minted key, where "go deeper" would mean four more multiple-
choice questions and a destination.

**Lesson authoring (LLM-generated lessons).** Easiest thing to build, least valuable thing to
have. Any chat window explains idempotency in fifteen seconds. The scarce thing is knowing
it's your problem, with receipts.
