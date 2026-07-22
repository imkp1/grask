# Changelog

Notable changes, newest first. This project is pre-1.0: anything may change, and the
SQLite schema under `GRILL_HOME` carries no migration guarantee yet.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) once there is a 1.0 to be compatible with.

## Unreleased

### Fixed

- **Install instructions were wrong, and broke `/grill`.** The README said `uv sync` puts
  `grill` and `grill-hook` on PATH; it installs them into `.venv/bin`. Both delivery
  surfaces invoke `grill` by name, so the skill's documented failure path — "grill is not
  installed on this PATH" — was what a correctly-following user actually got. The install
  is now `uv tool install .`, and the hook command is bare `grill-hook` rather than an
  absolute path into a checkout that can move.

### Added

- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- Issue templates for bugs and for bad questions, a pull request template, and Dependabot.
- A CI `packaging` job that installs via `uv tool install .` and runs both console
  scripts, so the class of breakage above cannot return unnoticed.
- `llms.txt`.

### Changed

- CI now runs `uv sync --locked`, so lockfile drift fails rather than resolving silently.
- `docs/skill/SKILL.md` moved to `skill/SKILL.md`. It is a shipped surface, not
  documentation, and filing it under `docs/` said otherwise. If you installed the skill by
  copying from the old path, nothing about your installed copy changes.

### Removed

- `docs/plans/` — 7,210 lines of task breakdowns, roughly 2.5× the size of `src/`. They
  cite line numbers that have moved and modules that no longer exist. The reasoning worth
  keeping is in `docs/design/`; the execution scaffolding is in git history.

## 0.1.0

First working end-to-end version. Capture pipeline, storage, both delivery surfaces
(`grill` in a terminal and `/grill` in Claude Code), and mechanical grading.

Not built: resurfacing a missed question, and cross-session dedup.
