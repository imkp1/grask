# Changelog

Notable changes, newest first. This project is pre-1.0: anything may change, and the
SQLite schema under `GRASK_HOME` carries no migration guarantee yet.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) once there is a 1.0 to be compatible with.

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
