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
from importlib import resources
from pathlib import Path

from grask.ask import (
    ERROR,
    FAILED,
    LETTERS,
    PASSED,
    PREMISE_REJECTED,
    SKIPPED,
    Console,
    _unservable,
    grade,
    resolution,
)
from grask.ask import (
    ask as _ask,
)
from grask.install import doctor, hook_configured, install, uninstall
from grask.storage import Store

NOTHING_PENDING = "nothing to ask about."

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
    text = (resources.files("grask") / "SKILL.md").read_text(encoding="utf-8")
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


def _serve(store_factory) -> int:
    """Print the next servable probe as JSON, blind: no key, no explanation.

    Consumes nothing — an abandoned Claude session leaves the probe pending,
    matching Ctrl-C in the terminal path. The one write is the same one `ask`
    keeps: a row too broken to grade is recorded as an error so it stops
    blocking the queue, and the loop moves to the next row.
    """
    with store_factory() as store:
        while True:
            pending = store.next_probe(max_options=MAX_UI_OPTIONS)
            if pending is None:
                print(json.dumps({"pending": None}))
                return 0
            if _unservable(pending):
                store.record_ask(resolution(pending, ERROR))
                continue
            print(
                json.dumps(
                    {
                        "probe_id": pending.probe_id,
                        "question": pending.question,
                        "options": list(pending.options),
                        "topic": pending.rubric.topic,
                        "created_at": pending.created_at,
                    }
                )
            )
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
        if _unservable(pending):
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

    out: dict[str, object] = {"outcome": interrogation.outcome}
    if interrogation.outcome in (PASSED, FAILED):
        out["explanation"] = pending.explanation
    print(json.dumps(out))
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
            # for the tools that push; this one was asked for.
            print(NOTHING_PENDING)
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
