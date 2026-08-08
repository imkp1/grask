# grask — Design

How grask works and why each part is shaped that way. This is the current document: when
the code and this disagree, one of them is a bug.

Every paragraph here states something true of the system as it stands. Where a measurement is
why a claim is weak, the number survives and the run does not — "n=3 detects nothing" is a
property of the current confidence; how the arms were interleaved is a method, and methods
belong in the pull request that ran them.

## Premise

From the inside, understanding something and having watched it happen are
indistinguishable — reading about idempotent retries and nodding along feels identical to
knowing it. grask finds out which one it was, by asking a question you can't bluff.

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

**Capture and delivery are split: push to capture, pull to deliver.** The design once fired
the question at session end. It does not: the hook runs detached, the developer has already
walked away, and a prompt written into a closing terminal is a prompt nobody reads. Capture
happens when the evidence is freshest; the question waits, for at most seven days, until the
developer asks for it. The split is also what makes the question cost nothing at the
moment it is generated: nobody is sitting there while four model calls run.

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
   transcript.py ─0─▶ triage.py ─1─▶ select.py ─▶ seed.py ─2─▶ probe.py ─3─▶ verify.py ─4─▶ SQLite
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

The hook drops a payload whose transcript is not on disk rather than spawning for it.
`SessionEnd` fires for every session including grask's own — each `claude -p` stage *is* a
session — and those run with `--no-session-persistence`, so the path in their payload never
becomes a file. Spawning anyway bought three processes and an `error` row per capture,
describing nothing that went wrong. The same shape appears whenever a transcript is moved or
cleaned up between the session ending and the worker starting, which the log shows happening
on its own.

**A session being captured is a state, not a gap.** `capture.py` writes a `capturing` row
before the first model call and replaces it with the real verdict at the end. Without it the
~45s the pipeline spends in four sequential model calls was a window in which an ended
session was indistinguishable from one that never happened — so `/grask` in a still-open
window answered "you're caught up, more after your next session" about a probe that was
seconds from existing, and the one action it recommended was the one that does not help. The
marker is believed for `CAPTURE_STALE_MINUTES` (30, against a worst case of 24 minutes of
stage timeouts); past that a worker is assumed dead, and the row stops both claiming a probe
is coming and blocking a re-capture of its session. It is also the one row a verdict may
overwrite — every other session row stays immutable, which is what stands between a re-fired
hook and paying for the same session twice.

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
| `sessions` | one row per session seen, whatever the outcome — `ask` \| `silent` \| `declined` \| `unverified` \| `error` |
| `seeds` | stage 2's topic, verified quotes, refs, decision, hypothesis |
| `probes` | the question, shuffled options, `correct_idx`, explanation |
| `asks` | one row per probe answered, `UNIQUE(probe_id)` |
| `answers` | the option text the developer picked |

**Silence and failure are recorded, not just keeps** — the keep- and failure-rates are the
signal that says whether any of this works, and recording every session is also what makes
capture idempotent: a `session_id` already present means we have seen it. `UNIQUE(probe_id)`
on `asks` makes an answer permanent, which is why Ctrl-C records nothing rather than a skip —
a stray keypress must not consume the question.

### Entry points

| Command | Purpose |
|---|---|
| `grask` | Ask the next pending question in the terminal. The product. |
| `grask serve --json` / `grask record <id>` | Machine-readable pair behind `/grask`; also the delivery test harness. |
| `grask skill [--install] [--dir]` | Write the `/grask` skill into a skills directory. |
| `grask install` / `grask uninstall` | Wire (or remove) the skill and the `SessionEnd` hook in `~/.claude`. The standalone path; the plugin does the same on install. |
| `grask doctor` | The one diagnostic — skill, hook, `claude`, and `uv`. Exit 1 on any failure. The skill is checked against the packaged copy, not just for existence: `install` copies it and an upgrade does not re-copy, so "present" was passing a skill written for an older grask. |
| `grask-hook` | The `SessionEnd` capture trigger. Registered by `grask install` in `settings.json`, or by the plugin's `hooks.json`; never invoked by hand. |

There is no `grask <topic>` entry point: a hand-typed topic is the one path where the fatal
failure — misreading code the developer actually wrote — cannot occur, so a probe validated
that way would measure a quality that does not transfer. The corpus runner
(`grask.capture_run`) exercises the same core against real transcripts instead, and
`grask.reprobe` re-runs stages 3 and 4 over seeds whose question never survived, and
`grask.triage_run` reports what stage 1 keeps across the whole corpus. All three are
module entry points rather than `grask` subcommands, and all three spend nothing without
`--go`: they are for whoever is developing grask, not for whoever installed it. A corpus
runner that bills on invocation is $10 nobody asked for.

### Distribution

Two install surfaces over one unchanged core. They are not two products: each wires the same
`SessionEnd` capture hook and the same `/grask` skill, and differ only in how grask's code is
located and run.

**Plugin (recommended).** The repository is also a Claude Code plugin — `.claude-plugin/`
holds the plugin and marketplace manifests, `hooks/hooks.json` the hooks, `skills/grask/` the
skill. `/plugin install grask` registers everything in one step, no `settings.json` editing and
no separate `pip`. The `SessionEnd` hook runs `env PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/src"
python3 -m grask.hook`: the plugin carries grask's source under `src/`, and grask runs on plain
`python3`. That is the whole runtime — no `uv`, no virtualenv, no build step. It is affordable
because grask has **no third-party dependencies** and reaches the model by running the `claude`
binary, not through an SDK; the one thing it needs from the environment is a Python new enough
(`≥ 3.8`), which `grask doctor` gates on. The plugin owns its code and runs it directly, rather
than assuming a separately-installed `grask` on PATH.

*Why not `uv`?* An earlier design wrapped every hook in `uv run --project`, which pinned Python
3.12 but dragged a virtualenv, a build, and a `SessionStart` pre-warm invented solely to hide the
cold-resolution latency at session exit. For a zero-dependency package that shells out to
`claude`, that machinery paid for a runtime grask does not have. Dropping it removed the venv,
the pre-warm, and `uv` from the check surface — replaced by one honest `python3 ≥ 3.8` gate in
`doctor`. Provisioning the interpreter is now the user's job (or their package manager's), which
is the same deal every other Python tool offers.

*The runner shim carries the plugin root to the skill.* A `SessionStart` hook runs
`grask shim --root "${CLAUDE_PLUGIN_ROOT}"`, which writes an executable shim to
`${GRASK_HOME}/grask` that re-enters grask exactly as the SessionEnd hook does —
`env PYTHONPATH="<root>/src" python3 -m grask.cli`. The `/grask` skill has to call grask too, but
`${CLAUDE_PLUGIN_ROOT}` is substituted only inside `hooks/hooks.json`, never in a skill's shell,
and the plugin puts no `grask` on PATH — so without this bridge the skill has no way to reach the
plugin's grask. The skill prefers the shim and falls back to a PATH `grask` for the standalone
install. The shim is rewritten every session because the plugin root carries a version and moves
on upgrade — and its presence is also how `doctor` recognises a plugin install and counts the
skill and capture hook as provided.

**Standalone (`grask install`).** For the bare `grask` command line, or for anyone who would
rather not run a plugin: `uv tool install grask`, then `grask install`. It writes the skill and
**idempotently** merges the `SessionEnd` hook into `~/.claude/settings.json` — a second install
adds nothing, and hooks other tools put there are preserved. `grask uninstall` reverses both and
leaves the database alone. The merge is the one delicate part, because it edits a file the
developer did not ask grask to own; every way it could corrupt that file is pinned in tests.

`grask install` validates only grask's own surfaces. It does **not** check for the `claude`
binary or a live authentication: someone may install grask before Claude, or authenticate
later, and failing an otherwise-correct install on that would be wrong. Those are environmental
concerns, and diagnosing them belongs to `grask doctor`.

**Diagnostics have one owner.** Hooks never speak — a hook that raised as the developer left
would surface a scary message about a tool they cannot see. So `grask doctor` is the canonical
diagnostic (`claude` present, `uv` present, hook wired, skill installed), and interactive
surfaces reuse its checks rather than duplicating them: a bare `grask` with capture unwired
nudges toward `grask install`. `doctor` reports as structured checks and exits non-zero on any
failure, so it is usable in CI and pasteable into a bug report.

*Namespacing.* Plugin skills are prefixed by the runtime, so the plugin path exposes the skill
as `/grask:grask` rather than a bare `/grask`. This is a runtime quirk, not part of grask's
identity, and may change if the runtime later allows aliasing; the standalone install keeps the
bare `/grask`.

*SKILL.md has one source.* The canonical file is `src/grask/SKILL.md` — the wheel reads it
through `importlib.resources` — and the plugin needs its own copy at `skills/grask/SKILL.md`. A
CI test asserts the two are byte-identical. A tracked symlink would be more elegant and less
robust: zip archives, Windows, and checkouts without symlink support all break it, so the boring
copy wins.

## The questioning engine

The project succeeds or fails here.

### Central risk: the question might not be worth asking

**The question is the product. Everything else is support.** A bad question is an uninstall:
*"what is a retry policy"* tells the developer instantly that this thing has nothing to
teach them, and no downstream quality recovers that.

So the bar is not "a relevant question." It is a question that makes an experienced
engineer stop for thirty seconds and go *"…huh"* — every session, from their own code.
Whether an LLM clears that bar reliably is the bet this project is making. It is measured,
not argued: see "Evaluating question quality".

This and developer motivation are the two risks that outrank everything below. Developer
motivation — whether anyone answers at all — is a product risk, not an implementation one,
and lives in `IDEA.md`.

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

**"A plausible wrong mechanism" says what a distractor must be, never what wrongness is made
of**, and that gap is where the burden this design accepted was quietly not being carried. So
the prompt names the kinds: a common misconception about the named technology, a nearby
mechanism that is real but not the one at work, cause and consequence swapped, the right
mechanism at the wrong time or layer or scope, two similar APIs confused for one another, an
invariant assumed to hold that nothing guarantees. And it names the three that hand back the
free pass — absurd, technically unrelated to what the question asks, or the correct option
with a qualifier removed. All three are eliminable without knowing the mechanism, and a pass
bought that cheaply reads exactly like a pass that was earned, which is what makes it worse
than a failed probe. This is the same lever "Limitations" reaches from the other end: if
passes turn out cheap, the fix is better distractors, not the judge's return.

**It has not been shown to work.** A blind A/B at n=3 detected no difference between this
block and the bare "plausible wrong mechanism" it elaborates — the honest reading is that the
shorter line was already carrying most of this weight. It is kept because ten lines cannot be
ruled out at that sample, not because an effect was observed. The yes-rate is what would
settle it, as it is for every other question about probe quality.

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

It is also the riskiest object here, because it is the hallucinated premise from
"Limitations" given a name. Naming it does not reduce that risk — it makes it inspectable.

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
                                                       result_block: verdict + key
                                                            + explanation
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

**A question labelled `explained_it_back` is demoted to `asked_why`, not dropped.** This was
the only gate firing on the corpus — 3 of 7 rank-0 proposals across 168 sessions — and each
rejection emptied its session, because those sessions had no other moment. Dropping was
throwing away a moment rank 1 would have kept: a question is exactly what `asked_why`'s own
gate requires, so the evidence is present and only the label was wrong. Demotion is safe in
the direction that matters — rank 0 is the claim the quote failed to support, so the moment
must never keep rank 0's precedence, and the worst case is a question about the topic the
developer asked about, which is what rank 1 does anyway. The two other rank-0 rejections stay
rejections: an empty `shows` leaves no claim about what was misunderstood, and there is no
weaker signal it satisfies. `Moment.relabelled_from` records the move, because folded silently
into `asked_why` the mislabel rate — the number that says whether stage 1 separates the two
signals — is unrecoverable.

**Pasted prose is not the developer's account, and only the prompt can say so.** A rank-0
moment was lost to a stage-2 decline whose session had 2,173 characters of pasted agent report
in turn 0 and turns of 3–34 characters everywhere else. The quote was verbatim in a developer
turn, so the evidence rule passed; it was not something the developer believed. Rank 0 is the
signal most exposed to this, because prose explaining a mechanism is what the signal looks
like. There is no structural fix: Claude Code inlines pasted text as an ordinary string —
exactly one transcript in the corpus carries a `Pasted text` marker — and a length heuristic
would fire on developers who write long prompts. So stage 1 is told what the tell is, and
that instruction is **unmeasured**. It failed safe in the observed case, since stage 2 caught
it, and the cost was one paid call rather than a bad question.

**Every rejection is recorded, not only the ones that emptied a session.** `demoted_from_ask`
answers only "did this session lose *all* of its moments". It names no gate, and says nothing
about a session that kept one moment and threw another away — which is most of
them — leaving a signal that never reaches a verdict indistinguishable from
one gated too strictly, a prompt bug and a gate bug respectively. So `TriageVerdict.rejections`
carries every rejection whatever the verdict, and `triage_run` reports surviving moments by
signal — including the signals at zero, because one absent from a report reads as *not
measured* rather than *never fired* — beside a tally of rejections by reason with the
`turn N:` prefix stripped, or one gate firing on four turns reads as four unrelated one-offs.

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

### One renderer, two styles

Everything a developer sees after a pick comes out of `ask.result_block`. The terminal
prints it; `record` returns it as `display` and the `/grask` skill prints that verbatim.
No surface composes a result from parts.

This is not tidiness. Two surfaces rendering one design from loose fields drift the first
time either is edited, and they did: the terminal printed a verdict and an explanation on
one line while the skill put a bare `✗` on a line of its own, which the developer read as
saying nothing about whether they were right. The model is a delivery surface for the
result exactly as it is for the question — it relays a string it cannot restructure.

The two styles differ only where the surrounding surface differs. The terminal already
printed the topic above the question and still has the options on screen, so `plain` omits
the topic and names the wrong pick by letter alone. The skill withheld the topic until the
answer was settled and its picker is gone, so `markdown` carries the topic and spells both
options out in full. Order, wording, and which outcome says what are shared.

**The verdict is a word, not only a glyph**, and it says nothing beyond what a pick can
support: `Correct` and `Incorrect` describe one pick against one key. Nothing here is a
score, and the rule above — one probe cannot identify understanding — is why there is no
streak, no total, and no adjective.

**A wrong pick is told which option was right, in full.** The explanation states the
mechanism without naming a letter, which leaves the developer mapping prose back onto a
picker that has already closed. Revealing the key here is safe in a way it never is in
`serve`: the row is written, `UNIQUE(probe_id)` refuses a second answer, and the question
is over.

**Skipping shows the answer too, with no verdict.** A skip is usually "I don't know" —
precisely when the payoff is worth most — and the probe is spent either way. `skipped`
stays its own stored outcome; it is not a wrong answer.

**A rejected premise gets neither.** The developer's claim is that the question is wrong.
Answering it with its own answer key argues past them, so the block is the outcome line and
nothing else.

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
✓ Correct

Backoff decides how long each client waits. It does nothing about them all waiting
the same amount. Clients dropped by one outage come back in lockstep, so the recovering
service takes the same thundering herd on every cycle. Jitter decorrelates the schedules.
```

Had they picked `a`, the same block names what they missed — by letter, since the options
are still on screen a few lines up:

```
> a
✗ Incorrect · you picked a, the answer was b

  b) Clients knocked out together retry together; jitter spreads them back out.

Backoff decides how long each client waits. It does nothing about them all waiting
the same amount. …
```

The context line is one line and mandatory. Without it the developer reads a question about
work they cannot place, which is the version of this tool that feels like a quiz.

**No topic ever takes an adjective.** The topic is a heading and, eventually, a scheduling
key. A string like `retries: hollow` asserts something one probe cannot support, and it is
banned. It is easy to be careful about this in prose and sloppy in the output format, which
is the only place the developer actually reads.

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

## Capture

Claude Code `SessionEnd` hook. Reads the transcript, runs five stages cheapest-first, writes
what survives, exits. Fails silently.

### Five stages, one invocation

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

**Stage 2 — seed (`seed.py`, one call).** Name, as a falsifiable claim, the most plausible
mechanism misconception the session's evidence supports, plus the topic, the verified quotes,
the `file:line` refs, and the decision that shipped. Stored and re-runnable; see
"Limitations".

**The hypothesis is a claim about evidence, not about a mind.** grask observes turns, agent
replies, and diffs. It never observes understanding, and "state what the developer does not
understand" asks the model to assert a hidden mental state from evidence that cannot carry
one — which it will do, fluently, on any session at all. The claim the evidence *can* support
is narrower and is the one stage 3 actually needs: the belief this session gives reason to
think is mistaken. The prompt names what is not evidence for that (the agent introduced the
mechanism, the developer asked a question, the developer accepted a suggestion, the pattern
looks unfamiliar) because each is evidence only that something happened.

**Stage 2 may decline, and a decline is not an error.** Triage decides a session is worth
*looking at*. It does not decide a misconception is there, and stage 2 is the first stage
that can tell — it is the first to see the agent's side of the conversation and the diff. So
it can answer `{"decline": "..."}` and the session records `declined`. Without that exit a
stage 2 that found nothing had two: invent a hypothesis, which produces a question that
teaches the developer the tool is guessing, or trip a structural gate and be recorded as a
broken pipeline. `declined` is deliberately neither `error` (nothing malfunctioned, and the
error rate is what says whether the prompt works) nor `silent` (triage kept this session, and
the count of sessions stage 2 talked it out of is the only way to see the decline collapsing
yield rather than trimming it). Unlike `unverified` it is invisible to `empty_reason`: no
question was ever written, so from the queue's side there is nothing to explain.

**Stage 3 — probe (`probe.py`, one call).** Write one multiple-choice question about the
mechanism, with the answer key and the explanation. Reads the full dialogue — turns, agent
replies, and the before/after text of edits — not just the seed.

**Why stage 3 reads the transcript and not the seed.** A compressed seed is enough to name
the topic; it is not enough to name the file, flag, or identifier that actually shipped, and
a question that cannot do that is a generic question. At ~0.8 KB of human input per session
there is no cost side to this tradeoff.

**Stage 4 — verify (`verify.py`, one call).** Read the question and its options *without*
the key, the explanation, the seed, or the transcript, and judge each option true or false on
its own with a stated reason. The probe survives only if exactly one option is true and it is
the stored key. Anything else — two true, none true, one true that is not the key — discards
the question and records the session `unverified` — a state `/grask` reports only while no
later session has minted a probe, because "the last question was thrown away" stops being
true the moment a newer one is not.

**Answerability is the judge's first question.** Before judging options it decides whether
the *question* can be answered from general technical knowledge plus the premises the stem
itself states. A stem may name a local file, flag, or identifier as its setting — that is the
anchor every good probe has — but if choosing between the options needs the contents of a
local file the question does not quote, no option is a true answer and the judge marks them
all false. This makes a rejection the design already wanted into one the log can explain.
Both wrong discards in the 47-probe backtest were exactly this shape: the judge took the
stem's premises as given, reasoned on top of them, preferred a false option, and the probe
was discarded for a reason that had nothing to do with what was wrong with it. The verdict
was right and the diagnosis was noise. The no-option-true message now carries every option's
reason for the same purpose — without it, "the mechanisms are all broken" and "only its
author could answer this" are the same string.

**What it costs: 3 of 15.** Three probes in fifteen that had already passed stage 4 under the
previous prompt fail the answerability check, all three local-file recall — the shape "the
question must teach something portable" already forbids and which nothing before this reliably
caught. The twelve kept are anchored on real files too, so the check separates an artifact
used as the setting from one the answer depends on. A ~20% yield cut on the current corpus,
and a bug report against stage 3 rather than a price stage 4 charges: those probes should
never have been written.

**Why a check and not more prompt.** Stage 3 writes the question, all four options, the key,
and the explanation in one call, in sequence, and never re-reads an early option against a
later one. Every failure that produces is already forbidden in the stage 3 prompt, so more
instruction is the approach that has been tried. Being generated from the session does not
make an option true either: the transcript supplies the *subject*, the model's own knowledge
supplies the mechanism, and "make distractors plausible" pushes toward true statements.

**Why it takes a `Probe` and nothing else.** A judge that has seen the reasoning behind the
answer is the model agreeing with itself. Keeping the seed and the dialogue unavailable at
the type level is what stops that eroding into a rubber stamp — the signature is the control.
The judge is also told it has no tools and no repository: given none, two of 47 backtested
probes answered with an attempted `bash` call instead of JSON rather than reason from the
question's own premises.

**Why it discards instead of arbitrating.** The original design repointed `correct_idx` when
the judge named a single true option that was not the key. Backtested over the 47 stored
probes that judgment landed three times: once on a genuinely false key (a PEP 503 question
whose key claimed `fault-line` normalises onto `faultline` — it does not), and twice on
questions about local files, where the judge could only reason from the stem's premises and
the option it preferred was false. Repointing would have fixed one and installed a falsehood
in two. It is also the wrong instinct on its own terms: an option a careful reader cannot
separate from the key is not a question worth twenty seconds either way.

**Why a call failure keeps the probe.** Only a judgment discards. Verification checks a
question that already exists, so a CLI that cannot run has said nothing about it — treating
silence as rejection would empty the queue every time the model was unreachable.

**Measured.** Over the 47 stored probes: 44 verified, 3 discarded (6%), 0 with two true
options. $0.041 per probe against a $0.226 stage 3 baseline, or about +10% on a kept
session's $0.51 — the invariant that keeps it affordable is that exactly one call per probe
carries the dialogue, and the rendered dialogue is 92% of stage 3's prompt.

**A discard is not free, and the row says so.** There is no probes row on this path, so
stage 3 and stage 4's spend has nowhere to live — and the report that decides whether stage 4
earns its price is the one that would have shown it as $0.00. `sessions.discarded_usd` holds
it, beside `cost_usd` rather than inside it, for exactly one reason: `SUM(discarded_usd)` is
what this stage has spent to produce nothing, and that is the number that decides whether it
is kept, tuned, or reverted. Merged into `cost_usd` it could not be recovered — separating it
back out would mean subtracting a per-session triage cost that is no longer stored anywhere.
The tidier-sounding argument, that `cost_usd` must stay summable as triage spend, is not the
reason and was not true when it was first written down: nothing reads that column as
triage-only. One column, one question that needs an exact answer.

**The seed survives, and something uses it.** A discard keeps the seed: the moment was real,
triage and stage 2 are already paid for, and what failed is the question written on top of
them. That is only a defence if the seed can be redeemed, so `reprobe.py` re-runs stages 3
and 4 over seeds that have no probe — the discards, and the sessions where stage 3 gave up
after stage 2 succeeded. Both stages again, not a shortcut past the check: these are the
seeds most likely to fail it a second time, and a second discard is a fact about the seed
rather than a bad roll. Explicit and cost-gated behind `--go`, for the reason `capture_run`
is: a retry folded into the next capture would bill a decision nobody made, and a seed that
fails twice would do it on a schedule. Bounded to `PROBE_TTL_DAYS`, because a probe born
expired is a model call spent on something `next_probe` will never serve, and skipped
entirely when the transcript has rotated — stage 3 needs the dialogue, not just the seed.

**The reason is stored; the retry is still blind, and that is measured.** `sessions.discard_reason`
holds why a session that had something to ask about produced no question: what stage 4 said
when it threw the question away, or what stage 2 said was missing when it declined to write a
seed. Both previously reached only `grask.log` — rotated at 1 MB, written by a detached
worker, readable by nothing — which left "a second discard is a fact about the seed" resting
on an attempt nobody had told what went wrong with the first, and left a rising decline rate
with no way to tell stage 2 declining correctly from stage 2 declining everything.

Feeding it back into stage 3 is not built. On the only sample available — the two discarded
probes whose transcripts survived — a blind re-run recovered both, so the premise that a retry
needs telling what went wrong did not hold, and n=2 is not evidence *for* threading a
`correction` parameter through three modules. Unproven code costs more than unproven prose.
The column stays regardless, populated for discards and declines: the judgment is the only
record of why a session produced no question, and filtered to `unverified` it is how the
locality rate gets measured at all. The verdict alone does not scope that query — `reprobe`
clears neither the verdict nor the reason when a retry succeeds, so a rate counted off
`verdict = 'unverified'` counts the redeemed seeds too; excluding them is the same
`LEFT JOIN probes ... WHERE p.id IS NULL` that `unprobed_seeds` uses to find them.

**What it cannot check.** The judge has no repository, so it can only adjudicate claims that
are true away from this checkout. A probe whose answer turns on the contents of a local file
is one it must take on the stem's premises — which is the same probe "portable past this
repository" already rules out under "What one probe can and cannot say". Both of the wrong
discards above were that shape, so the discard rate is also a weak signal of the question
being too local to be worth asking.

**Why one invocation.** The transcript is the fragile input: transcript files rotate, and the
diff a seed references drifts as the branch moves. Reading it once, at the moment it is
freshest, is worth more than the work saved by deferring.

**Why stage 3 is not lazy.** Deferring question generation to `grask` invocation would only
produce questions for sessions someone chose to open — a biased sample of the one thing most
in need of unbiased measurement. Rejected on those grounds, not on cost.

### What counts as a topic

The governing question, from which everything else follows:

> **What evidence suggests the developer accepted something without fully understanding it?**

The taxonomy is a set of detectors for that invariant and is subordinate to it. If a signal
stops serving the invariant, the signal goes — it does not get to redefine the principle by
being the thing that happens to be implemented.

Five signals, defined in `triage.py` and ranked in `select.py`. The split that matters is
**whether a quote can prove the signal at all**:

| Rank | Signal | Evidence | What it is |
|---|---|---|---|
| 0 | `explained_it_back` | quote-provable | They put the mechanism in their own words and got it wrong. |
| 1 | `asked_why` | quote-provable | They asked why. Their curiosity, not the agent's output. |
| 2 | `pushed_back` | quote-provable | They corrected, overrode, or disagreed. Judgment showing. |
| 3 | `new_pattern` | code-grounded | A pattern, library, or technique newly landed in their code. |
| 4 | `explained_at_length` | code-grounded | The agent explained at length and they took it on board. |

For the first three, the developer's own words *are* the evidence — a why-question, a
correction, or a mechanism restated wrongly is visible in the quote itself. For the last two
the quote can only ever be circumstantial: a pattern landing in the codebase is shown by the
code, not by anything the developer typed. Those are kept but flagged `weak_evidence`, and
stage 2 has to ground them in the dialogue before they earn a question.

**The ranking is derived, not asserted:** signals whose quote is self-proving come first,
because preferring the others would make selection favour the weakest evidence available.
The shorthand is **quiz what they were told, not what they told the agent** — a question they
asked means an answer they received that nobody checked.

**`explained_it_back` outranks even `asked_why`, because of what the evidence is evidence
*of*.** Everything below rank 0 is a reason to suspect a gap; rank 0 is a gap. A why-question
shows curiosity, and curiosity is perfectly compatible with having understood the answer. A
mechanism restated wrongly *is* the misconception, in the developer's own words, with the
wrong part visible. It was also the hole in the original four: `asked_why` is gated on the
quote being a question, and a confident wrong statement corrects nobody, so it was not
`pushed_back` either — the strongest evidence the transcript can hold had no signal to land
on. The evidence rule is correspondingly stricter for it, and structurally so rather than by
prompting: the quote must not be a question — a developer asking how something works is
`asked_why` — and `shows` must be non-empty, because that is where the part they got wrong or
left out gets named, and a moment claiming a misconception without naming one is a moment
that would take the session's question with nothing behind it. The rule this module already
lives by is that prompting a model to require a quote is not a control; checking the quote is.
The highest-ranked signal is the last one that should have been exempt from it.

**Demonstrated understanding disqualifies a moment.** The signals say what happened at a
point in the session; a later turn can take it back. If the developer states the mechanism
correctly in their own words at any point after the moment, the moment is dropped. A
developer who asks "why do we need jitter here?" and later says "right, so the clients don't
all retry at the same instant" has supplied the strongest evidence available that there is no
gap here — and asking them anyway spends the session's one question on the topic with the
most evidence they already know it. This bites hardest on `new_pattern` and
`explained_at_length`, where nothing the developer typed was evidence of a gap to begin with.

It bites on rank 0 too, and that is the case the wording has to be careful about, because it
is the modal `explained_it_back` transcript: restate the mechanism wrongly, get corrected, say
it back correctly. Read as an exemption — "a wrong restatement is `explained_it_back`, and
that is the best moment in the session" — the rule would hand the session's one question to
the mechanism the developer demonstrably just learned, which is precisely what it exists to
prevent. A wrong restatement earns rank 0 only while nothing later in the session shows they
now have it right.

`rank_key` is `(signal_rank, -turn)`: signal first, then the latest turn, because with signal
equal the more recent engagement is the one still fresh when the question arrives. It depends
only on the moment itself, never on the other candidates, which is what keeps the winner
stable when extraction adds or drops a marginal moment between runs — and it does.

**Prefer mechanisms over process.** Both stage 1 and stage 2 down-rank behavioural moments —
why a message was phrased a certain way, workflow or etiquette choices, why the agent took
the approach it took. A mechanism has a right answer a question can test; a process choice
mostly does not.

**The signal chooses the question's shape.** Stage 3 used to ask every moment the same way —
"what does this API return" — which was the wrong question for half the signals that reach
it. `pushed_back` and `asked_why` are precisely the moments where the developer's *judgment*
was engaged: they argued with a proposal, or wanted to know why. Asking them to recite a
decision they made deliberately is the least interesting thing available. Those two get a
consequence frame — a counterfactual ("X is keyed on Y; if Y stopped being unique, what
breaks first?"), a constraint attribution ("which constraint forces the shuffle to happen at
storage time?"), or the cost of the road taken. `new_pattern` and `explained_at_length` really
are recall — something went past, or the agent explained and it was accepted — and keep the
mechanism frame. So does `explained_it_back`, despite outranking both judgment signals: the
developer has already told you what they believe the mechanism is and they were wrong about
it, so the mechanism is the thing to ask about.

**Rank 0 also chooses one of the distractors.** Everywhere else stage 3 invents its wrong
options from the taxonomy of wrongness above — educated guesses at what a half-understanding
would look like. `explained_it_back` is the one signal where the session already contains
one: the developer's own account of the mechanism, stated in the transcript and known to be
wrong. The frame requires it to be an option. This is the same lever "The answer is a pick"
identifies as the only place left to catch a fluent answer describing a different mechanism,
loaded with something better than a guess — a developer who half-understood this would pick
it, because one of them did.

Two constraints keep it from backfiring. It is **paraphrased into a general claim about the
mechanism, never quoted**: the design refuses to show the developer their own mistake, which
is why the hypothesis is internal, and an option reading as *your words, wrong* is the
accusation grask does not make. And it must be **false**, like every other distractor —
rank 0 covers accounts that were incomplete as well as wrong, and an incomplete account is
often true as far as it goes, which is exactly the probe stage 4 discards for having two
true options. An incomplete account is therefore sharpened into the false general claim it
would imply if taken as the whole mechanism, rather than restated.

It is prompt-only, and not a fifth structural gate. The four gates are all mechanically
decidable; "is this option the developer's stated mechanism" is a semantic judgement, and
checking it in code means a model call in the capture path, which is a judge by another
name.

**It has not been shown to work.** Rank 0 fires on about one kept session in nine (4 of 38,
retrospectively triaged over the corpus), and a paired A/B on three of those seeds recovered
the misconception 3 of 3 with this block against 2 of 3 without it. At n=3 that detects
nothing, and the honest reading is that stage 3 was already doing most of this from the
hypothesis alone. Kept on the same terms as the distractor-shape block above.

**Threading `Moment.shows` through to stage 3 is the alternative, and is not built.** Half the
surviving rank-0 accounts are *incomplete* rather than wrong, where the misconception is not
in the quote at all — the case the extra field looks necessary for. Stage 2 covers it: a
hypothesis has to be falsifiable, so it already restates an omission as a positive false
belief, which is the form stage 3 needs, and the field would duplicate work done a stage
earlier. What would justify threading it is a rank-0 distractor that comes out invented rather
than recovered, and not before.

What does not vary is the invariant: exactly one correct option, grounded in the named
artifact, portable past this repository. A frame chooses the shape of a question, never
whether it can be graded. "Which approach is nicer" has no answer key, and a probe without a
key cannot exist in a design with no judge — see "The answer is a pick, not an explanation".
This is the guard the unbuilt `definition_gap` re-cut below already names, applied to the
taxonomy that actually shipped.

Explicitly not qualifying: files edited, commands run, tests made to pass, config changes,
dependency bumps, renames, lint fixes, a bug the developer diagnosed themselves, writing
prose or commit messages, or a session where the developer only said "continue" and "fix it".
That is activity, and activity is not learning.

**A proposed re-cut, mostly unbuilt.** An intent-shaped taxonomy — `definition_gap` ("what is
X"), `asks_rationale` ("why X"), `counter_proposal` ("why not Y"), `asserts_belief`
("…right?") — splits the current `asked_why` into four and orders them by strength of
acceptance evidence. Its `asserts_belief` arm is the one piece that shipped, as
`explained_it_back`, because it was the arm the four-signal taxonomy could not express at
all; the rest is a finer cut of moments that already have a signal. It is not implemented;
`triage.py` and `select.py` are the current taxonomy. It should not be built before the yes-rate exists,
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

- Python 3.8+, uv. No runtime dependencies.
- SQLite. Local file, no server.
- CLI + a `SessionEnd` hook + a Claude Code skill, shipped both as a plugin and as a `uv tool`
  install. No HTTP, no frontend, no build step. See "Distribution".
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
earlier draft reserved both; transcripts turned out tiny — ~0.8 KB of human input per session
— so no stage needs a cheaper model and none is offered, and an adapter with one
implementation is a layer, not a design.

**Each `claude -p` call is stripped down.** `--disable-slash-commands` drops the user's skill
listing from the inherited context — the largest part of it. `--tools ""` removes the built-in
tools rather than forbidding them, so a stage sends one self-contained prompt and gets one JSON
object back rather than wandering into the repo as an agent; `--disallowed-tools` refused the
same calls but still spent the tokens describing every tool it was refusing (8,918 → 5,585
input tokens on a trivial prompt, $0.0048 → $0.0031). `--no-session-persistence` stops each
call writing a transcript of its own. `--bare` would cut more and is rejected: it reads auth
strictly from `ANTHROPIC_API_KEY`, which breaks the "no second credential" property above.

These flags are savings, not requirements, and they run where a non-zero exit reaches nobody.
A CLI that does not recognise one would fail every capture silently, so an
unrecognised-option failure demotes the call to the flag set that has always worked and
remembers the demotion for the rest of the process.

**None of it is a latency fix.** Any wall-clock difference between these flag sets is inside
API variance (±700ms call to call). Capture's ~45s is four sequential model calls, three of
them over a ~15k-token dialogue, and no flag here touches that; the only lever left is
structural — merging stages 2 and 3 into one call, which would trade the quote-verification
boundary between them for about ten seconds, and is not taken. Prompt-prefix caching does not
help either: across two `claude -p` processes only the system prefix is reused (3,260 tokens)
and the user message is cache-written whole every call, so ordering the stage prompts to share
a prefix buys nothing.

## Failure modes

Two invariants make the rest fall out: **`capture.py` never raises** — nothing watches its
exit code, so every failure becomes a row and a log line — and **the hook always returns 0**,
so an unparseable payload is logged and swallowed and grask never speaks on the way out. Every
other failure — a triage call that fails (recorded `error`, not `silent`), a stored row too
malformed to grade (served as `error` and consumed so it stops blocking the queue), a
misread session (`/wrong` → `premise_rejected`), a mid-question Ctrl-C (records nothing) —
resolves to one of the recorded outcomes rather than to an exception.

## Testing

243 tests, no network, no model: every path that would call a model takes the callable as an
argument — the injected console in `ask.py`, the injected stages in `capture_session` — so the
whole pipeline is exercised against scripted inputs. One `calibration` test runs the real
pipeline against a real model and is deselected by default because it costs money. What the
tests **cannot** cover is whether the questions are any good — that is the north-star metric,
and it needs the vote.

## Evaluating question quality

grask stores no grade, score, or per-topic verdict: one probe identifies at most one
misconception and cannot measure understanding (see "What one probe can and cannot say").
The only metric that matters is whether the question was worth asking — and it is **not
built**, the largest gap between this document and the code.

The intent is one binary after the explanation, where it costs nothing in the moment it
would distort:

```
was this worth asking?   [ y / n ]
```

**Yes-rate is the north-star** — the direct instrument for the central risk, whether an LLM
reliably clears the "…huh" bar. Skip rate cannot stand in for it: a skip conflates *bad
question* with *busy developer*, two things that need opposite responses. The vote must not
violate the design that produced it — one keypress, skippable, never framed as feedback on
the developer, and it never asks *why*. Until it exists, quality has only two weak proxies —
the `premise_rejected` and `skipped` rates — and the author reading probes.

## Limitations

**Answer-key quality caps everything.** A wrong key marks a correct pick incorrect, with a
confident explanation and no judge left to blame. Mechanical grading did not remove this
ceiling — it moved it earlier, from answer time to generation time, where it is at least
inspectable and re-runnable. Grounding in real code raises the stakes: a question about a
concept is wrong only if the model is ignorant, but a question about *this interface in your
codebase* requires the model to have inferred correctly why the interface exists, and when it
infers wrong the developer is marked wrong against a premise that is simply mistaken. Three
things blunt it — the hypothesis is stored (the first thing to read when a question feels
off), a high `premise_rejected` rate is a bug report against grounding, and seeds are
re-runnable into a better probe when the stage-3 prompt improves.

**Whether recognition is enough is unresolved.** Multiple choice removed the judge and both
of its failure modes, but a pick is weaker evidence than an explanation. If passes turn out
cheap — developers eliminating three options without understanding the mechanism — the fix is
better distractors, not the judge's return. If that is not enough, the judge question reopens
for real.

**Grounding reads whole files, not the session diff.** Switching to the diff is the one
deferred cost optimization, held until question quality is known, because a probe may need
surrounding context the diff omits.

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
