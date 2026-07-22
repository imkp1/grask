# Security

grask reads your Claude Code transcripts and sends parts of them to a model. That is its
function, not a defect — but it means the interesting security questions here are about
data handling rather than about remote attackers. This file states what the tool does, so
you can tell a bug from the design.

## What grask does with your data

- **Reads every transcript under `~/.claude/projects/`**, across every repository on the
  machine. It is not scoped to one project. `--root` points it elsewhere; `--exclude`
  skips projects by name substring.
- **Sends transcript content to a model** — your prompts, the agent's replies, and the
  before/after text of edits — by shelling out to `claude -p`. That call runs under your
  existing Claude Code authentication and is subject to whatever data policy your account
  already has.
- **Stores what it extracts locally**, in SQLite under `~/.claude/grask/` (or
  `$GRASK_HOME`), including verbatim quotes of things you typed.
- **Sends nothing anywhere else.** There is no telemetry, no analytics, no phone-home, and
  no second credential. grask has zero runtime dependencies, so there is no third-party
  package in the path of your data.

If a repository you work in is covered by an agreement that prohibits sending source to a
model, do not install the hook.

## What counts as a vulnerability

Please report privately:

- Data written outside `GRASK_HOME` — into a working tree, a temp file that outlives the
  run, or any shared location.
- Transcript content reaching a destination other than the `claude` CLI.
- A path where grask executes content from a transcript rather than treating it as text.
  Transcripts are attacker-influenced input if you ever paste untrusted material into a
  session, and grask's prompts embed them.
- Anything that makes the `SessionEnd` hook block, crash visibly, or interfere with the
  session that spawned it.
- Any way to read another user's grask database through the tool.

Not vulnerabilities, by design: that grask reads all projects (documented above), that it
sends content to a model (that is the product), and that the database is unencrypted at
rest (it is protected by your filesystem permissions, like the transcripts it derives
from).

## Reporting

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**), which opens a channel only the maintainer can
see. Please don't open a public issue for anything in the list above.

Expect an acknowledgement within a week. grask is a solo alpha project — there is no
on-call rotation and no SLA, and it is better to say that than to imply one.

When you report, **do not paste raw transcript content or `grask.log` output**. Describe
the shape of the problem and redact the rest.

## Supported versions

Alpha, pre-1.0: only `main` is supported. There are no backports.

## Hardening notes

Two properties are worth knowing if you are assessing grask for use somewhere careful:

- **Model calls run with tools disallowed and slash commands disabled**
  (`NO_TOOLS`, `NO_SKILLS` in `src/grask/llm.py`). grask sends one self-contained prompt
  and expects one JSON object back; the model cannot read your repository during a grask
  call.
- **The hook cannot block or shout.** It parses the payload, spawns a detached worker, and
  returns 0 unconditionally. Every exception is swallowed to the log — a hook that raises
  as you leave a session produces a frightening message about a tool you cannot see.
