"""`grask` — ask me the question.

The only module here that owns a terminal. Everything it knows about interaction
lives in `TerminalConsole`; everything it knows about interrogation it delegates
to `ask.py`, which has never heard of a TTY. That split is what keeps the
delivery question open: a hook, a nudge, or a prompt injection would replace this
file and nothing else.

Deliberately not registered in settings.json. This is invoked by hand.

`serve`/`record` are the non-interactive delivery seam this file's docstring
reserved, driven by the `/grask` skill rather than a human at a TTY.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from grask.ask import (
    ERROR,
    LETTERS,
    MARKDOWN,
    PREMISE_REJECTED,
    SKIPPED,
    Console,
    grade,
    resolution,
    result_block,
    unservable,
)
from grask.ask import (
    ask as _ask,
)
from grask.install import (
    doctor,
    hook_configured,
    install,
    packaged_skill_text,
    uninstall,
    write_runner_shim,
)
from grask.storage import PROBE_TTL_DAYS, Store

NOTHING_PENDING = "nothing to ask about."

# The terminal's version of `EMPTY_QUEUE_NOTES`, in this surface's voice: one
# lowercase line, keyed by the same `Store.empty_reason`. `over_cap` is
# unreachable here — the terminal calls `next_probe` with no cap and can ask a
# row of any width — so it is carried only so the lookup cannot raise.
TERMINAL_EMPTY_NOTES = {
    "never": (
        f"{NOTHING_PENDING} grask writes probes from a session's transcript "
        "after that session ends, so nothing is queued until one closes."
    ),
    "caught_up": f"{NOTHING_PENDING} you're caught up — more after your next session.",
    "capturing": (
        "a session just ended and its question is still being written — "
        "about forty-five seconds. try again shortly."
    ),
    "unverified": (
        f"{NOTHING_PENDING} the last question grask wrote did not survive its "
        "own check — it could not confirm the answer key, and threw the question "
        "away rather than grade you against it."
    ),
    "expired": (
        f"{NOTHING_PENDING} what was queued went unasked for over "
        f"{PROBE_TTL_DAYS} days and expired."
    ),
    "over_cap": NOTHING_PENDING,
}

# Claude's native question UI takes at most 4 options; rows over the cap are
# left pending for the terminal path rather than consumed.
MAX_UI_OPTIONS = 4

# Where Claude Code looks for user-level skills. A skill is one directory
# holding one SKILL.md, and the directory name is the slash command — so this
# has to end in `grask/` for `/grask` to exist.
DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _skill(args: argparse.Namespace) -> int:
    """Print the shipped `/grask` skill, or write it into a skills directory.

    The file ships inside the package, so this works identically from a clone
    and from an installed wheel. Telling the user to copy a path out of a
    checkout only ever worked for people who had a checkout.
    """
    text = packaged_skill_text()
    if not args.install:
        print(text, end="")
        return 0

    target = (args.dir or DEFAULT_SKILLS_DIR) / "grask" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"installed {target}")
    return 0


class TerminalConsole:
    """The real console: print and `input`.

    `EOFError` becomes an empty string, which the loop already reads as a skip —
    Ctrl-D is a deliberate "not now". `KeyboardInterrupt` is deliberately NOT
    caught here; `main` handles it by recording nothing at all.
    """

    def show(self, text: str) -> None:
        print(text)

    def prompt(self, text: str) -> str:
        try:
            return input(f"{text}\n> ")
        except EOFError:
            return ""


MORE_LATER = "More arrive after your next session ends."

# One note per `Store.empty_reason`. An empty queue is the first thing a new
# install shows and the most common thing a caught-up one shows, so each state
# says which it is — "nothing pending" alone reads like a failure, and a single
# shared line would misdescribe five of these six.
EMPTY_QUEUE_NOTES = {
    "never": (
        "Nothing is queued. grask writes probes from a session's transcript "
        "after that session ends, so a new install has nothing to ask until at "
        "least one session has closed — empty by construction, not broken."
    ),
    "caught_up": f"You are caught up: every probe grask raised is answered. {MORE_LATER}",
    "capturing": (
        "A session ended in the last few minutes and grask is still writing its "
        "question — the pipeline is four model calls and takes about forty-five "
        "seconds. Nothing is wrong and nothing is lost; the probe will be here "
        "shortly. Do not end another session to hurry it along."
    ),
    "unverified": (
        "Nothing is queued. The last question grask wrote was discarded before "
        "it reached you: a second pass reads the options without knowing which is "
        "the key, and it could not confirm that exactly one of them was true. A "
        "question graded against an answer key that might be wrong is worse than "
        f"no question. Nothing is broken and nothing needs doing. {MORE_LATER}"
    ),
    "expired": (
        f"Nothing is queued. The probes grask had raised went unasked for more "
        f"than {PROBE_TTL_DAYS} days and expired — a probe about work you no "
        f"longer remember is a quiz, not a check. {MORE_LATER}"
    ),
    "over_cap": (
        f"Nothing this surface can ask. The probes still waiting carry more than "
        f"{MAX_UI_OPTIONS} options, which is the native question UI's limit, so "
        f"they are left for the terminal: run `grask` in a shell to answer them."
    ),
}


def _next_payload(store, *, max_options: int | None = MAX_UI_OPTIONS) -> dict[str, Any]:
    """The next servable probe as a JSON-ready dict, or why there isn't one.

    The one place that shape is built. `serve` prints it and `record` embeds it
    as `next`, so the model never has to learn two ways of reading "what is
    waiting" — and, more to the point, never has to spend a second Bash call and
    a second model turn to find out. `serve` is 60ms; the turn around it is
    seconds, and the turn is what the developer actually waits through.

    Consuming nothing is preserved: an abandoned Claude session leaves the probe
    pending, matching Ctrl-C in the terminal path. The one write is the same one
    `ask` keeps: a row too broken to grade is recorded as an error so it stops
    blocking the queue, and the loop moves to the next row.
    """
    while True:
        pending = store.next_probe(max_options=max_options)
        if pending is None:
            reason = store.empty_reason(max_options=max_options)
            return {
                "pending": None,
                "reason": reason,
                "note": EMPTY_QUEUE_NOTES[reason],
            }
        if unservable(pending):
            store.record_ask(resolution(pending, ERROR))
            continue
        return {
            "probe_id": pending.probe_id,
            "question": pending.question,
            "options": list(pending.options),
            "topic": pending.rubric.topic,
            "created_at": pending.created_at,
        }


def _serve(store_factory) -> int:
    """Print the next servable probe as JSON, blind: no key, no explanation.

    The payload and its consume-nothing behaviour are `_next_payload`'s; this is
    the surface that prints it. An empty queue carries `reason` and `note`
    because the first `/grask` of a new install always lands here, and "nothing
    pending" alone reads like a failure. `over_cap` in particular has to be
    distinguishable: the terminal path can still ask those rows.
    """
    with store_factory() as store:
        print(json.dumps(_next_payload(store)))
    return 0


def _shim(args: argparse.Namespace) -> int:
    """SessionStart's one job: write the runner shim so the `/grask` skill can
    reach *this* plugin's grask without a `grask` on PATH. The skill has no
    `${CLAUDE_PLUGIN_ROOT}` of its own, so the hook — which does — bakes the root
    into the shim here, every session, since the root moves on upgrade.

    It must never fail a session open, so a shim it could not write is a warning
    on stderr, not a non-zero exit.

    Silent on success, and that is the point: a SessionStart hook's stdout is
    injected into the session's context and shown to the developer, so a success
    line here is a notification about plumbing, in every session, forever —
    charged to the context window of work that has nothing to do with grask.
    Only the failure has anything to say, and it says it on stderr."""
    try:
        write_runner_shim(args.root)
    except OSError as exc:
        print(f"shim: could not write runner shim: {exc}", file=sys.stderr)
    return 0


def _fail(message: str) -> int:
    """A domain error Claude can parse: JSON on stdout, non-zero exit, no write."""
    print(json.dumps({"error": message}))
    return 1


def _record(args: argparse.Namespace, parser: argparse.ArgumentParser, store_factory) -> int:
    """Record one answer non-interactively. Exactly one of pick / skip / wrong.

    Flag misuse is argparse's problem (usage error, exit 2); everything about
    the stored data — unknown id, already answered, letter out of range — is a
    JSON error, because that is the half Claude cannot know before calling.
    """
    if args.skip and args.wrong:
        parser.error("--skip and --wrong are mutually exclusive")
    if (args.skip or args.wrong) and args.pick is not None:
        parser.error("--pick only makes sense when answering")
    if not (args.skip or args.wrong) and args.pick is None:
        parser.error("answering needs --pick")
    if args.objection is not None and not args.wrong:
        parser.error("--objection only makes sense with --wrong")

    with store_factory() as store:
        pending = store.probe_by_id(args.probe_id)
        if pending is None:
            return _fail(f"no servable probe with id {args.probe_id}")
        if unservable(pending):
            return _fail(
                f"probe {args.probe_id} is malformed; `serve` records those as errors"
            )

        if args.skip:
            interrogation = resolution(pending, SKIPPED)
        elif args.wrong:
            interrogation = resolution(pending, PREMISE_REJECTED, objection=args.objection)
        else:
            try:
                interrogation = grade(pending, args.pick)
            except ValueError as exc:
                return _fail(str(exc))

        try:
            store.record_ask(interrogation)
        except sqlite3.IntegrityError:
            # UNIQUE(probe_id): the row is permanent, so a second record is a
            # refusal, not an overwrite.
            return _fail(f"probe {args.probe_id} was already answered")

        # Answered inside the same open store, and the same process, as the
        # write above: what is pending next is knowable here for free, and the
        # `serve` call the skill used to make for it cost a Bash round-trip and
        # a model turn — seconds, against 60ms of actual work.
        #
        # This is not auto-serving. The payload is inert until the skill asks
        # the developer whether to continue; per-probe consent stays an explicit
        # tap, as "Restraint" requires. What is removed is the wait, not the
        # question.
        upcoming = _next_payload(store)

    # Three fields, and only one of them is for reading. `display` is the whole
    # result, rendered here rather than composed by the model: a surface handed
    # loose parts formats them differently every time it is edited, which is how
    # the skill once printed a bare `✗` on a line of its own. `explanation` is
    # not returned alongside it — a second copy is a second thing that can
    # disagree with the first.
    print(
        json.dumps(
            {
                "outcome": interrogation.outcome,
                "display": result_block(pending, interrogation, style=MARKDOWN),
                "next": upcoming,
            }
        )
    )
    return 0


def main(
    argv: list[str] | None = None,
    *,
    store_factory=Store,
    ask=_ask,
    console: Console | None = None,
    check_hook=hook_configured,
) -> int:
    """Take one pending probe, interrogate, record. Returns a shell exit code."""
    parser = argparse.ArgumentParser(
        prog="grask", description="Answer one question about something you shipped."
    )
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser(
        "serve", help="print the next pending probe as one JSON object"
    )
    serve_parser.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="emit JSON (the only mode; the flag keeps the contract explicit)",
    )

    record_parser = sub.add_parser(
        "record", help="record an answer to a probe served elsewhere"
    )
    record_parser.add_argument("probe_id", type=int)
    # Case-folded before the choices check. The delivery surface labels options
    # with letters and echoes back whatever it displayed, so `--pick A` is the
    # normal thing to send, not a typo — argparse rejecting it stranded a real
    # answer the developer had already given.
    record_parser.add_argument(
        "--pick", type=str.lower, choices=list(LETTERS[:MAX_UI_OPTIONS])
    )
    record_parser.add_argument("--skip", action="store_true")
    record_parser.add_argument("--wrong", action="store_true")
    record_parser.add_argument("--objection")

    skill_parser = sub.add_parser(
        "skill", help="print the /grask skill, or install it with --install"
    )
    skill_parser.add_argument(
        "--install", action="store_true", help="write it into a skills directory"
    )
    # Project-level skills live in `.claude/skills` next to a repo rather than
    # under $HOME, and that is a real setup, not just a test seam.
    skill_parser.add_argument(
        "--dir", type=Path, help=f"skills directory (default: {DEFAULT_SKILLS_DIR})"
    )

    sub.add_parser(
        "install", help="wire the /grask skill and the SessionEnd capture hook into ~/.claude"
    )
    sub.add_parser(
        "uninstall", help="remove grask's skill and capture hook (your data is left alone)"
    )
    sub.add_parser(
        "doctor", help="check grask's configuration and the environment it needs"
    )
    shim_parser = sub.add_parser(
        "shim",
        help="write the /grask runner shim for this plugin root (SessionStart)",
    )
    shim_parser.add_argument(
        "--root", required=True, help="the plugin root to run grask from"
    )

    args = parser.parse_args(argv)

    if args.command == "skill":
        return _skill(args)
    if args.command == "serve":
        return _serve(store_factory)
    if args.command == "record":
        return _record(args, record_parser, store_factory)
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    if args.command == "doctor":
        return doctor()
    if args.command == "shim":
        return _shim(args)

    # Bare `grask` with capture unwired: the package is installed but nothing is
    # feeding it. Say so once, on stderr so it never corrupts the answer flow, and
    # carry on — there may still be a probe from a manual capture to serve.
    if not check_hook():
        print(
            "grask captures nothing until it's wired up — run `grask install`.",
            file=sys.stderr,
        )

    with store_factory() as store:
        pending = store.next_probe()
        if pending is None:
            # A command you typed that prints nothing looks broken. Silence is
            # for the tools that push; this one was asked for — and one line for
            # four different states would misdescribe three of them.
            print(TERMINAL_EMPTY_NOTES[store.empty_reason()])
            return 0

        try:
            interrogation = ask(pending, console or TerminalConsole())
        except KeyboardInterrupt:
            # Record nothing. An `asks` row is permanent (UNIQUE on probe_id), so
            # writing one here would consume the probe on a stray keypress rather
            # than leaving it for the next run.
            print()
            return 130

        store.record_ask(interrogation)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
