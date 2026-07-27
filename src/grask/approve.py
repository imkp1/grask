"""The PreToolUse entry point: pre-approve grask's own two commands.

Without this, a `/grask` round costs two or three permission prompts — `serve`,
`record`, `serve` again — and the developer taps "yes" more times than they
answer questions. A plugin cannot ship permission rules, but it can ship a hook
that decides for its own commands, which is narrower anyway: the rule below
matches a parsed argv, not a prefix glob.

This runs before *every* Bash call in every session grask is installed in, so it
imports nothing from the rest of grask and does no I/O beyond stdin. Anything it
does not recognise it stays silent about — silence falls through to the normal
permission flow, which is the correct answer for every command that is not
grask's.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

# Anything that could turn one approved command into two. shlex would happily
# parse `serve --json; rm -rf ~` into tokens that pass every check below, so the
# metacharacters have to be refused before parsing, not after — and before, not
# after, quote removal, since `"$(...)"` survives shlex as inert-looking text.
# Glob and history characters are not here: they can widen one argument but they
# cannot start a second command, and an objection is the developer's own prose.
SHELL_METACHARACTERS = (";", "&", "|", "<", ">", "`", "$(", "\n", "\r")

# The literal spellings the skill is told to use, plus the resolved absolute
# path. `~` and `$HOME` never reach a shell here — they are compared as text.
SHIM_SPELLINGS = ("~/.claude/grask/grask", "$HOME/.claude/grask/grask")

# One entry per shape the skill runs. A flag mapped to True takes a value.
ALLOWED = {
    "serve": {"--json": False},
    "record": {"--pick": True, "--skip": False, "--wrong": False, "--objection": True},
}


def _shim_path() -> str:
    home = os.environ.get("GRASK_HOME")
    base = Path(home) if home else Path.home() / ".claude" / "grask"
    return str(base / "grask")


def is_grask_command(command: str) -> bool:
    """True only for a bare invocation of grask's shim with `serve` or `record`
    and flags those subcommands actually take. Deliberately literal: a command
    this does not recognise is not refused, it is left to the developer."""
    if not command or any(bad in command for bad in SHELL_METACHARACTERS):
        return False
    # `$HOME` is the one `$` allowed, and only as the whole first token, so it
    # is taken out before the remainder is checked for any other expansion.
    head, _, tail = command.partition(" ")
    if "$" in tail or ("$" in head and head != SHIM_SPELLINGS[1]):
        return False

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 2:
        return False

    binary, subcommand, *rest = tokens
    if binary not in SHIM_SPELLINGS and binary != _shim_path():
        return False
    flags = ALLOWED.get(subcommand)
    if flags is None:
        return False

    expecting_value = False
    for token in rest:
        if expecting_value:
            expecting_value = False
            continue
        if token.startswith("-"):
            if token not in flags:
                return False
            expecting_value = flags[token]
        elif subcommand != "record":
            # Only `record` takes a positional, and only one: the probe id.
            return False
    return not expecting_value


def decide(payload: dict[str, object]) -> dict[str, object] | None:
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", "")
    if not isinstance(command, str) or not is_grask_command(command):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "grask's own serve/record command",
        }
    }


def main(stdin=sys.stdin, stdout=sys.stdout) -> int:
    """Never fails a tool call. A hook that raises on the way to an unrelated
    `git status` is worse than a permission prompt on `/grask`."""
    try:
        decision = decide(json.load(stdin))
    except Exception:
        return 0
    if decision is not None:
        json.dump(decision, stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
