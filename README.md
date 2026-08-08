# grask

[![CI](https://github.com/imkp1/grask/actions/workflows/ci.yml/badge.svg)](https://github.com/imkp1/grask/actions/workflows/ci.yml)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#status)

One question about your own code, when you finish coding.

You can't tell the difference between understanding something and having watched it happen.
Shipping code used to force the issue — you couldn't ship what didn't work, and making it
work meant understanding it at least once. That forcing function is gone.

grask watches your Claude Code sessions end and usually asks nothing. When it does ask, it
asks exactly one multiple-choice question about the mechanism you shipped, the next time
you run `/grask:grask`.

> **Most sessions produce no question. That silence is the feature** — not a failure to
> find something.

![grask: I have code in production I couldn't explain — one multiple-choice question about the code you just shipped](docs/grask.gif)

## Example

`/grask:grask` asks this through Claude Code's native question UI, as in the recording
above. The same probe rendered as text, from the command the skill calls underneath:

```
$ ~/.claude/grask/grask
from 2026-08-08 · retry backoff in the webhook dispatcher

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

Grading is mechanical — the key was minted with the question, so there is no second model
call and no judge to argue with.

## Install

Needs the [Claude Code CLI](https://claude.com/claude-code) already installed and
authenticated, plus a `python3` (≥ 3.8) on your PATH. grask has no runtime dependencies and
needs no second API key — it shells out to the `claude` binary you already use.

Inside Claude Code:

```
/plugin marketplace add imkp1/grask
/plugin install grask
```

That is the whole setup — no `settings.json` editing, no `pip`, no `uv`. The plugin carries
grask's source under `src/` and runs it with plain `python3`; grask has no third-party
dependencies, so there is no virtualenv and no build step. It wires the `SessionEnd` capture
hook and the `/grask:grask` skill, and a `SessionStart` step writes a small shim at
`~/.claude/grask/grask` so the skill can find the plugin's copy of grask. If your `python3`
is older than 3.8, `~/.claude/grask/grask doctor` will say so.

The skill is namespaced by the runtime, so you type `/grask:grask`, not `/grask`.

### Updating

The plugin does not update itself, and it holds a *copy* of the skill — so a new release
reaches you only when you refresh that copy:

```
/plugin marketplace update grask
/reload-plugins
```

If the version still reads as the old one afterwards, the install is pinned to the version
directory it was installed from. From a shell, `claude plugin update grask@grask` repoints
it, and `claude plugin list` reports what you are actually on.

Claude Code disables auto-update for third-party marketplaces by default, and only ships an
update when the plugin's `version` field changes — which every grask release bumps, so the
plugin cache path changes with it and you get a clean refetch rather than a silent no-op.

The capture hook spawns a detached worker and returns immediately — it never blocks the end
of your session, and it never speaks. Failures go to `~/.claude/grask/grask.log`, never to
your terminal. If capture ever seems off, `~/.claude/grask/grask doctor` is the one place
that will tell you why.

### The two commands it runs

There is nothing to approve. The plugin ships a `PreToolUse` hook that approves
exactly `~/.claude/grask/grask serve` and `… record`, with the flags those two
take, and stays silent about every other command in your session. Without it a
single probe would cost two or three approval taps — `serve`, `record`, `serve`
again — which is more taps than the probe has answers.

Those two are the skill's entire interface: `serve` prints the next question and
writes nothing, `record` grades one answer. Neither takes input from the model
beyond a probe id and your pick.

## Use

```
/grask:grask       # ask the next pending question, or say there's nothing
```

```bash
~/.claude/grask/grask stats     # what it has asked you, and how it went
~/.claude/grask/grask doctor    # what is wired, and what is not
```

You get one question, three or four options, and one line of orientation about which
session it came from. Pick an option. "Other" takes `skip`, or `wrong: <what's off>` if the
question misreads what happened.

Questions expire after 7 days. A probe about work you did last week is a quiz.

`stats` is read-only and free — it asks nothing and consumes nothing. It prints
counts and the questions you were recently asked, and deliberately no percentage: one
probe cannot identify understanding, so a score over a handful of them asserts something
grask does not know, and turns a twenty-second check into a number to protect.

## Privacy — read this before installing

grask reads your Claude Code transcripts, and it is not scoped to one project:

- **It reads every transcript under `~/.claude/projects/`**, across all your repositories.
- **It sends transcript content to a model** — your prompts, the agent's replies, and the
  before/after text of edits — by shelling out to `claude -p`. That call runs under your
  existing Claude Code authentication and is subject to whatever data policy your account
  already has. No data goes anywhere else, and grask adds no telemetry.
- **It stores what it extracts locally**, in a SQLite database at `~/.claude/grask/`,
  including verbatim quotes of things you typed.

Controls:

- `GRASK_HOME` relocates the database and log.
- The batch tools in [CONTRIBUTING.md](CONTRIBUTING.md#corpus-tools) take `--exclude` to
  skip projects by name substring, and `--root` to point at a different transcript
  directory. The session-end hook has neither — it captures whatever session just ended.

If any of your repositories are covered by an agreement that prohibits sending source to a
model, do not install the hook.

**Nothing derived from a real transcript belongs in this repository.** The tools write
under `GRASK_HOME` by default for exactly that reason, and `.gitignore` is a second line of
defence. This project has made that mistake once already.

## How it works

Five stages, cheapest first. Each one filters, so only what survives pays for the next.

| Stage | Module | Cost | Job |
|---|---|---|---|
| 0 — extract | `transcript.py` | free | Pull the developer's own turns out of a session log. Tool results, file snapshots, and injected skill text are not the developer thinking. Sessions with no human turns stop here. |
| 1 — triage | `triage.py` | one call | List every moment worth asking about, each anchored to a verbatim quote and the turn it came from. Sees turns and file *paths*, never file contents. Most sessions yield nothing. |
| — select | `select.py` | free | Rank the moments and pick one. Deliberately code, not prompt: a model asked to both find and choose picks arbitrarily, and the topic changed run to run on an unchanged session. |
| 2 — seed | `seed.py` | one call | State, as a falsifiable claim, what the developer may have accepted without understanding. Stored, so a better stage-3 prompt can re-ask the whole corpus later. |
| 3 — probe | `probe.py` | one call, up to 3 | Write one multiple-choice question about the mechanism, with the answer key and an explanation. A structurally unusable question is regenerated. |
| 4 — verify | `verify.py` | one call | Read the options *without* the key, the explanation, or the transcript, and judge each one true or false on its own. The probe is kept only if exactly one is true and it is the key; otherwise it is discarded. |

Several rules are enforced in code rather than prompted for, because instruction is not a
control. Two matter most:

- **The evidence rule.** A triaged moment whose quote does not appear in the turn it names
  is demoted to silence. Same for a seed quote that appears nowhere the developer typed.
- **The one-question rule.** A stem with two questions in it cannot have one correct
  option, so it is rejected and regenerated, up to three attempts.

The question must also teach something portable. A question whose answer is "because this
file says so" is answerable only by whoever sat through the session and is worth nothing
once they close the file.

- **The one-key rule.** Stage 3 writes the question, every option, the key, and the
  explanation in one pass and never re-reads an early option against a later one, so stage 4
  reads them back cold. Measured over 47 stored probes it discards 6% — including one whose
  key described a name collision that does not happen.

grask names no model: it calls `claude -p` with no `--model` flag, so every stage runs on
whatever you have selected and there is no second credential to manage. Its own prompts are
small, and stages 2 and 3 only run on the minority of sessions triage keeps. Measured costs
are in [`docs/design.md`](docs/design.md#five-stages-one-invocation), stage by stage.

## Status

Alpha, and honest about it. The capture pipeline, storage, the question UI, and
mechanical grading all work end to end.

Not built, in the order they matter: the one-keypress *"was this worth asking?"* vote, which
is the only planned measure of whether the questions are any good; cross-session dedup, so
two sessions can currently produce near-identical probes; and resurfacing a question you got
wrong, which is the half [`IDEA.md`](IDEA.md) argues matters most.

[`docs/design.md`](docs/design.md) has the reasoning behind each decision;
[`IDEA.md`](IDEA.md) covers what this is and the ways it might not work.

## Contributing

Issues and pull requests are welcome — [CONTRIBUTING.md](CONTRIBUTING.md) has the
development setup, the three checks CI runs, and the three rules that are easy to break by
accident. Also [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

A question grask asked that was *bad* — wrong premise, wrong key, or testing nothing
portable — is the single most useful thing you can report. There is an issue template
for exactly that.

For anything where grask leaked, over-collected, or wrote outside `GRASK_HOME`, see
[SECURITY.md](SECURITY.md) and report it privately rather than in a public issue.

## License

MIT — see [LICENSE](LICENSE).
