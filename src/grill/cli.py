"""`grill` — ask me the question.

The only module here that owns a terminal. Everything it knows about interaction
lives in `TerminalConsole`; everything it knows about interrogation it delegates
to `ask.py`, which has never heard of a TTY. That split is what keeps the
delivery question open: a hook, a nudge, or a prompt injection would replace this
file and nothing else.

Deliberately not registered in settings.json. This is invoked by hand.

`serve`/`record` are the non-interactive delivery seam this file's docstring
reserved, driven by the `/grill` skill rather than a human at a TTY.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from grill.ask import (
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
from grill.ask import (
    ask as _ask,
)
from grill.storage import Store

NOTHING_PENDING = "nothing to ask about."

# Claude's native question UI takes at most 4 options; rows over the cap are
# left pending for the terminal path rather than consumed.
MAX_UI_OPTIONS = 4


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
) -> int:
    """Take one pending probe, interrogate, record. Returns a shell exit code."""
    parser = argparse.ArgumentParser(
        prog="grill", description="Answer one question about something you shipped."
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

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(store_factory)
    if args.command == "record":
        return _record(args, record_parser, store_factory)

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
