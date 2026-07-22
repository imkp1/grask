# Claude slash-command delivery

2026-07-22. Serves probes inside Claude Code via `/grill`, using Claude's
native question UI instead of a separate terminal.

## Problem

First real run of the multiple-choice flow: the terminal UX is too bad. A
separate `uv run grill` session, raw `input()` prompts, and typed letters are
more friction than anyone will accept. The developer already lives inside
Claude Code, which has a native multiple-choice question UI.

## Decisions

Made with the user, 2026-07-22:

- **On-demand only.** `/grill` serves the next pending probe when typed. No
  session-start nudge, no proactive serving.
- **Blind serve.** Claude never sees `correct_idx` or `explanation` before the
  pick. Grading happens in grill, not in Claude.
- **Probes capped at 4 options.** Claude's question UI takes at most 4 options.
  Generation emits 3–4; the terminal path keeps a–e for legacy rows, which the
  skill will not serve.
- **Confidence commit survives, two rounds.** 95/70/40 committed in its own
  round before the pick, preserving commit-before-answer.

## Design

Grill gains two non-interactive subcommands; Claude drives them from a
user-level skill. `ask.py` and the interactive `grill` command are untouched —
this is the delivery seam `cli.py`'s docstring reserved.

### `grill serve --json` (`cli.py`)

Prints the next servable pending probe as one JSON object:

```json
{"probe_id": 2, "question": "...", "options": ["...", "..."],
 "topic": "...", "created_at": "2026-07-21T..."}
```

- Never includes `correct_idx` or `explanation`.
- Consumes nothing. No `asks` row is written; an abandoned Claude session
  leaves the probe pending, matching Ctrl-C semantics in the terminal path.
- Empty queue → `{"pending": null}`, exit 0.
- Malformed row (fails `_unservable`): record `outcome = error` for it and
  advance to the next, same policy as `ask()`.
- Row with more than 4 options: skip it in the query
  (`json_array_length(options) <= 4`) and leave it pending — it is unservable
  for this path only, and the terminal can still serve it. Recording `error`
  would consume a valid row.

### `grill record <probe_id>` (`cli.py`)

Exactly one of:

- `--confidence {95,70,40} --pick {a,b,c,d}` — grades mechanically
  (`pick == correct_idx`), writes the `asks` row via the existing
  `record_ask`, prints `{"outcome": "passed"|"failed", "explanation": "..."}`.
- `--skip` — records `skipped` (with `--confidence` if the skip came after
  the commit).
- `--wrong [--objection TEXT]` — records `premise_rejected`.

Errors (unknown probe id, already answered via the UNIQUE constraint on
`asks.probe_id`, pick letter out of range for the stored options) → JSON
error on stdout, non-zero exit. No partial writes.

Construction reuses `ask.py`'s vocabulary (outcome constants, `Interrogation`,
`AnswerTurn`) — no duplicate grading logic. The pure function that maps
(pending, confidence, pick letter) → `Interrogation` lives in `ask.py` beside
`ask()`; `cli.py` only parses args and prints JSON.

### Generation cap (`probe.py`)

Structural gate changes from 3–5 options to 3–4. Prompt text updated to match.
`LETTERS = "abcde"` and the terminal path are unchanged.

### Skill (`~/.claude/skills/grill/SKILL.md`)

User-level so `/grill` works in any project. Instructs Claude to:

1. Run `uv run grill serve --json` from `~/projects/grill`. `pending: null` →
   say so, stop.
2. Render the context line, stem, and lettered options as markdown.
3. Round 1 — native question, options `95`, `70`, `40`, `Skip`. The rare
   premise-rejection goes through the built-in free-text "Other" field
   (documented in the option descriptions).
4. Round 2 — native question, options `a`–`d`: letter as label, full option
   text as description. Skip/wrong again via "Other".
5. Run `uv run grill record <id>` with the answers. Show ✓/✗ + explanation.
6. Run `serve` again; if another probe is pending, offer to continue —
   don't auto-serve.
7. Never open the grill database directly; never speculate about the correct
   answer before `record` returns.

### Testing

- `serve`: emits the pending probe without `correct_idx`/`explanation`
  (asserted on raw output text); consumes nothing (probe still served next
  call); empty queue shape; >4-option row left pending and skipped;
  malformed row → `error` recorded and next row served.
- `record`: passed/failed verdicts against a known row; skip; wrong with and
  without objection; double-record rejected; bad pick letter rejected; JSON
  shapes exact.
- The (pending, confidence, pick) → `Interrogation` function: scripted unit
  tests in the existing zero-spend style.
- Generation gate: 5 options now rejected.

### Out of scope

- Any nudge/hook delivery (revisit after `/grill` sees real use).
- Regenerating stored 5-option probes (none pending today; they age out).
- Retiring the terminal path.
