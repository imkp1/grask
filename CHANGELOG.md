# Changelog

Notable changes, newest first. This project is pre-1.0: anything may change, and the
SQLite schema under `GRASK_HOME` carries no migration guarantee yet.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) once there is a 1.0 to be compatible with.

## 0.1.0-rc6

- **A probe's answer key is checked before the probe reaches you.** Stage 3 writes the question,
  every option, the key and the explanation in one call, in sequence, and never re-reads an early
  option against a later one; every failure that produces is already forbidden in its prompt, so
  more instruction is the approach that has been tried. A fourth stage reads the options back
  cold — no key, no explanation, no seed, no transcript, no repository — and judges each one true
  or false. The probe survives only if exactly one is true and it is the stored key. Withholding
  the transcript is enforced at the type level, because a judge that has seen the reasoning
  behind an answer is the model agreeing with itself. Backtested over all 47 stored probes: 44
  verified, 3 discarded, including a key that claimed `fault-line` normalises onto `faultline`
  under PEP 503, which it does not. It costs $0.041 a probe, about a tenth on top of a kept
  session. A judgment discards the question and keeps the seed; a call that could not run keeps
  the question, because a CLI that never answered has said nothing about it, and reading silence
  as rejection would empty the queue every time the model was unreachable. Discarded sessions get
  a sixth empty-queue reason of their own — not `silent`, not `error` — and the spend behind a
  thrown-away question is recorded rather than lost.
- **A session being captured is now a state the queue can name.** Ending one window and
  running `/grask` in another said "you're caught up — more after your next session" about a
  probe that was thirty seconds from existing: capture writes nothing until all three of its
  model calls finish, so an ended session looked exactly like a session that never happened,
  and the one action the message recommended was the one that does not help. The worker now
  marks the session `capturing` before it spends anything, and both surfaces have a fifth
  empty-queue reason that says to wait rather than to go end something. A marker older than 30
  minutes is a dead worker: it stops promising a probe and stops blocking a re-capture. Verdicts
  may overwrite that marker and nothing else — every other session row is still immutable, which
  is what keeps a re-fired hook from paying twice.
- **Stage 3 asks judgment moments a judgment-shaped question.** Every probe was framed as
  "what does this API return", including the ones raised because the developer pushed back on a
  proposal or asked why — recall, aimed at the two signals where judgment was the thing that
  happened. `pushed_back` and `asked_why` now get a counterfactual, a constraint attribution, or
  the cost of the road taken; `new_pattern` and `explained_at_length` keep the mechanism framing.
  The invariant is untouched: exactly one correct option, grounded in the named artifact,
  portable past the repo. A frame picks the shape of a question, never whether it can be graded.
- **`record` returns the next probe as `next`.** The skill used to call `serve` again after
  every answer. `serve` is 60ms of SQLite; the Bash round-trip and model turn wrapped around it
  are seconds, and that is what the developer actually sat through. Consent is unchanged — the
  payload is inert until the developer taps `Yes`, and nothing auto-serves.
- **`claude -p` calls carry `--tools ""` and `--no-session-persistence`.** Withholding the
  built-in tools rather than forbidding them cuts 8,918 → 5,585 input tokens and $0.0048 →
  $0.0031 per call, for definitions no stage was allowed to use. Not persisting a session stops
  each call writing a transcript that the hook then captured — 279 of the store's rows were
  grask reading its own model calls. Neither is a latency fix and the measurement says so: an
  apparent 0.6s saving did not survive n=3. A CLI that does not know these flags demotes to the
  old set rather than failing every capture where nobody would see it.
- **The hook drops payloads whose transcript is not on disk.** It fired for grask's own
  non-persisted `-p` sessions, and for transcripts moved or cleaned up before the worker
  started, spawning workers that could only write `error` rows about nothing.

## 0.1.0-rc5

- **The result of a pick is rendered in one place, and it names the answer.** The verdict was
  a bare `✓`/`✗` glyph and nothing ever said which option was correct — after a wrong pick you
  were left mapping an explanation back onto a picker that had already closed. Worse, each
  surface composed its own result from loose fields, so they drifted: the terminal printed a
  verdict and an explanation on one line while the `/grask` skill put a naked `✗` on a line by
  itself, which reads as saying nothing at all. Both now print `ask.result_block` and nothing
  else — `record` returns it as `display`, and the skill's job is to relay that string. The
  verdict is a word (`Correct` / `Incorrect`, never a score), a wrong pick is shown the correct
  option in full, and a skip gets the answer and the explanation too, since skipping is usually
  "I don't know" and the probe is spent either way. A rejected premise still gets neither:
  answering a disputed question with its own key argues past the objection.
- **`record`'s JSON is now `{outcome, display}`.** `explanation` is gone from the payload — it
  is inside `display`, and a second copy is a second thing that can disagree. `serve` is
  unchanged and still blind: the key appears nowhere until the row is written and
  `UNIQUE(probe_id)` has refused a second answer.

- **The plugin now approves its own two commands.** Spelling the shim literally was
  necessary but not sufficient: with permissions on, a literal command still prompts unless
  something has allowed it, so one probe cost two or three approval taps — `serve`, `record`,
  `serve` again — and rc4's fix was invisible to anyone who had not hand-edited
  `settings.json`. A plugin cannot ship permission rules, so grask ships a `PreToolUse` hook
  (`grask.approve`) instead, which is narrower than an allowlist glob: it parses the argv and
  approves only `serve` and `record` with the flags those two subcommands take, refuses any
  shell metacharacter that could join a second command, and stays silent — falling through to
  the normal prompt — for everything else it sees. The manual `allow` entries in the README
  are now only for standalone `pip install` users, who have no plugin hooks.

## 0.1.0-rc4

- **An empty queue now says which kind of empty it is.** `serve` returned a bare
  `{"pending": null}` and the terminal printed a bare `nothing to ask about.`, which reads
  like a broken tool on the first `/grask` of a fresh install. Both surfaces now report one
  of four states from the new `Store.empty_reason`: `never` (nothing captured yet, because
  probes are written after a session ends), `caught_up`, `expired` (probes went unasked past
  the 7-day TTL), and `over_cap`. `over_cap` is the one that was actively misleading: `serve`
  caps options at the native question UI's limit of 4, so a wider probe was reported as an
  empty queue while a bare `grask` in a terminal could still ask it.
- **The `/grask` skill calls its shim by literal path.** The skill opened with
  `GRASK="${GRASK_HOME:-$HOME/.claude/grask}/grask"`, and a command carrying `${...}` cannot
  be matched against Claude Code's permission rules, so every single call prompted for
  approval. A first-time user's first sight of grask was an opaque shell one-liner asking to
  be approved, before any probe. The happy path is now `~/.claude/grask/grask`, with the
  resolver kept as a documented fallback for standalone installs and `GRASK_HOME` overrides.
  The README also lists the two `allow` entries that approve `serve` and `record` for good.

## 0.1.0-rc3

- **Fix: the first `/grask` question call failed.** The skill described options as `label` +
  `preview`, but the question tool's schema requires `label` and `description` — `preview` is the
  optional one. A faithful first call omitted a required field and was rejected; the model only
  recovered by guessing a `description` on the retry. The skill now specifies all three fields and
  defines `description` as a mechanical continuation of the stored option text, so the new field
  cannot leak which option holds.

## 0.1.0-rc2

- **Runs on Python 3.8+ (was 3.12+).** The 3.12 floor was almost entirely accidental: the only
  hard blocker was `datetime.UTC` (a 3.11 alias for `timezone.utc`) plus one use of PEP 695 type
  syntax, both since removed. `grask doctor` now gates on `python3 ≥ 3.8`. This matters because
  the plugin runs on whatever `python3` the machine has — on a pyenv box that can be 3.9, where
  the old code crashed every hook on import. Ruff now targets `py38` so 3.9+ syntax cannot creep
  back in; the suite runs green on 3.8 and 3.14.
- **Plugin runtime no longer uses `uv`.** The plugin now runs grask with plain
  `env PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/src" python3 -m grask.…` — no virtualenv, no build,
  no `uv`. grask has no third-party dependencies and reaches the model through the `claude`
  binary, so the venv machinery (and the `SessionStart` pre-warm that only existed to hide its
  cold-start latency) paid for a runtime grask does not have. The one requirement is now a
  `python3 ≥ 3.8`, which `grask doctor` gates on in place of the old `uv on PATH` check.
  Standalone (`uv tool install grask`) is unchanged.
- **Fix: `/grask` under a plugin-only install.** The skill called a bare `grask`, which the
  plugin deliberately does not put on PATH — so `/grask` failed for anyone who installed the
  plugin without also `uv tool install grask`. `SessionStart` now writes an executable runner
  shim (`grask shim --root`) that the skill invokes; the skill falls back to a PATH `grask` for
  the standalone install.
- **`grask doctor` understands the plugin.** The runner shim is the plugin's fingerprint, so a
  plugin-only install no longer reports its skill and capture hook as missing.

## 0.1.0-rc1

First public version. Everything below is new, so this entry describes what exists rather
than what changed.

- **Capture.** A `SessionEnd` hook spawns a detached worker that runs four stages —
  extract, triage, select, seed, probe — and stores at most one question per session.
  Most sessions store nothing.
- **Delivery.** `grask` in a terminal, and `/grask` inside Claude Code via a skill that
  `grask skill --install` writes into place. Both invoke the same graded ask.
- **Grading.** Mechanical. The answer key is minted with the question, so answering costs
  no model call and there is no judge.
- **Storage.** SQLite under `GRASK_HOME` (default `~/.claude/grask/`). Seeds are stored
  separately from probes, so a better prompt can re-ask the whole corpus later.

Not built: resurfacing a missed question, and cross-session dedup — two sessions can
currently produce near-identical probes.
