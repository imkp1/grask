# grill

[![CI](https://github.com/imkp1/grill/actions/workflows/ci.yml/badge.svg)](https://github.com/imkp1/grill/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#status)

One question about your own code, when you finish coding.

You can't tell the difference between understanding something and having watched it happen.
Shipping code with an agent used to force the issue — you couldn't ship what you didn't
understand, because it wouldn't run. That forcing function is gone.

grill watches your Claude Code sessions end, decides whether anything in one was worth
asking about, and — usually not — stays silent. When something was, it mints a single
multiple-choice question about the mechanism you shipped, and asks it the next time you
run `/grill`.

Most sessions produce nothing. That is the system working, not a failure to find something.

## Install

Requires Python 3.12+ and the [Claude Code CLI](https://claude.com/claude-code) already
installed and authenticated. grill has no runtime dependencies and needs no second API
key — it shells out to the `claude` binary you already use.

```bash
git clone https://github.com/imkp1/grill && cd grill
uv tool install .
```

This puts two commands on your PATH: `grill` (ask a question) and `grill-hook` (the
capture trigger). `pipx install .` works the same way. Not on PyPI yet — install from a
checkout.

Installing as a tool rather than `uv sync` is deliberate: both surfaces below invoke
`grill` by name, so it has to resolve without a path. Use `uv sync` only for working on
grill itself.

### Wire up the hook

Capture runs when a session ends. Add to your Claude Code `settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "grill-hook" }
        ]
      }
    ]
  }
}
```

If you installed with `uv sync` instead, this has to be the absolute path to
`.venv/bin/grill-hook` — and it breaks the moment you move the checkout.

The hook reads the payload, spawns a detached worker, and returns immediately — it never
blocks the end of your session, and it never speaks. Failures go to
`~/.claude/grill/grill.log`, never to your terminal.

### Install the skill (optional)

Copy `skill/SKILL.md` into your skills directory as `grill/SKILL.md` to get `/grill`
inside Claude Code, using the native question UI. Without it, `grill` on the command line
does the same job in the terminal.

## Use

```bash
grill          # ask the next pending question, or say there's nothing
```

You get one question, three or four options, and one line of orientation about which
session it came from. Pick a letter. Grading is mechanical — the answer key was minted
when the question was, so there is no model call, no latency, and no judge to argue with.
`enter` skips. `/wrong` rejects the premise if the question misreads what happened.

Questions expire after 7 days. A probe about work you did last week is a quiz.

## Privacy — read this before installing

grill reads your Claude Code transcripts, and it is not scoped to one project:

- **It reads every transcript under `~/.claude/projects/`**, across all your repositories.
- **It sends transcript content to a model** — your prompts, the agent's replies, and the
  before/after text of edits — by shelling out to `claude -p`. That call runs under your
  existing Claude Code authentication and is subject to whatever data policy your account
  already has. No data goes anywhere else, and grill adds no telemetry.
- **It stores what it extracts locally**, in a SQLite database at `~/.claude/grill/`,
  including verbatim quotes of things you typed.

Controls:

- `GRILL_HOME` relocates the database and log.
- The corpus tools take `--exclude` to skip projects by name substring, and `--root` to
  point at a different transcript directory.

If any of your repositories are covered by an agreement that prohibits sending source to a
model, do not install the hook.

**Nothing derived from a real transcript belongs in this repository.** The tools write
under `GRILL_HOME` by default for exactly that reason, and `.gitignore` is a second line of
defence. This project has made that mistake once already.

## How it works

Four stages, cheapest first. Each one filters, so only what survives pays for the next.

| Stage | Module | Cost | Job |
|---|---|---|---|
| 0 — extract | `transcript.py` | free | Pull the developer's own turns out of a session log. Tool results, file snapshots, and injected skill text are not the developer thinking. Sessions with no human turns stop here. |
| 1 — triage | `triage.py` | one call | List every moment worth asking about, each anchored to a verbatim quote and the turn it came from. Sees turns and file *paths*, never file contents. Most sessions yield nothing. |
| — select | `select.py` | free | Rank the moments and pick one. Deliberately code, not prompt: a model asked to both find and choose picks arbitrarily, and the topic changed run to run on an unchanged session. |
| 2 — seed | `seed.py` | one call | State, as a falsifiable claim, what the developer may have accepted without understanding. Stored, so a better stage-3 prompt can re-ask the whole corpus later. |
| 3 — probe | `probe.py` | one call | Write one multiple-choice question about the mechanism, with the answer key and an explanation. |

Two rules are enforced in code rather than prompted for, because instruction is not a
control:

- **The evidence rule.** A triaged moment whose quote does not appear in the turn it names
  is demoted to silence. Same for a seed quote that appears nowhere the developer typed.
- **The one-question rule.** A stem with two questions in it cannot have one correct
  option, so it is rejected and regenerated, up to three attempts.

The question must also teach something portable. A question whose answer is "because this
file says so" is answerable only by whoever sat through the session and is worth nothing
once they close the file.

### Model selection

grill names no model. It calls `claude -p` with no `--model` flag, so every stage runs on
whatever you currently have selected, and there is no second credential to manage. Tools
are disallowed and slash commands disabled — grill sends one self-contained prompt and
wants one JSON object back, so letting it wander the repo would just cost money.

## Cost

grill's own prompts are small. The real cost is the Claude Code context inherited by
shelling out to `claude -p` at all, which scales with *your* configuration rather than with
grill — `--disable-slash-commands` exists in `llm.py` for precisely this reason and cuts a
call substantially on a large setup.

Stages 2 and 3 only run on sessions triage kept, which is a minority. The corpus tools
print an estimated range and refuse to spend without `--go`.

## Development

```bash
uv sync                # working on grill itself, rather than installing it
uv run pytest          # 221 tests, no network, no model calls, sub-second
uv run ruff check .
uv run mypy
```

The suite never calls a model. Every stage is injected as a plain callable, so the whole
pipeline is drivable by test doubles — that seam is why the tests are free and fast.
Tests marked `calibration` do call the real model and cost money; they are deselected by
default and run deliberately with `pytest -m calibration`.

### Corpus tools

Diagnostics for working on grill itself, not part of normal use. All of them write under
`GRILL_HOME`, never the working directory.

```bash
uv run python -m grill.survey       # what's in the local transcript corpus (free)
uv run python -m grill.triage_run   # run stage 1 over the corpus; costs money
uv run python -m grill.capture_run  # run the full pipeline over past sessions; --go to spend
```

`capture_run` skips grill's own project by default. The sessions that end from now on are
overwhelmingly grill's own, and waiting for the queue to fill measures grill on grill.

## Status

Alpha, and honest about it. The capture pipeline, storage, both delivery surfaces, and
mechanical grading all work end to end.

Not built: resurfacing a missed question, and cross-session dedup — two sessions can
currently produce near-identical probes.

Design notes and the reasoning behind each decision are in
[`docs/design/grill-design.md`](docs/design/grill-design.md); [`IDEA.md`](IDEA.md) covers
what this is and why it might not work. [`docs/README.md`](docs/README.md) says which of
those documents are current and which are records.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
three rules that are easy to break by accident, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

A question grill asked that was *bad* — wrong premise, wrong key, or testing nothing
portable — is the single most useful thing you can report. There is an issue template
for exactly that.

For anything where grill leaked, over-collected, or wrote outside `GRILL_HOME`, see
[SECURITY.md](SECURITY.md) and report it privately rather than in a public issue.

## License

MIT — see [LICENSE](LICENSE).
