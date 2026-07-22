"""Adapter for the one LLM the plugin is allowed to assume: the user's own.

The design names no model. The hook shells out to the already-authenticated
Claude Code CLI without a `--model` flag, so every stage runs on whatever the
developer currently has selected. See "Model selection" in the design doc.

This module is the only place that knows a subprocess is involved.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT = 180

# Triage reasons about text that is already in the prompt; it has nothing to look
# up. Granting tools would let it wander into the repo, which costs money and
# turns a fast classifier into an agent.
NO_TOOLS = (
    "--disallowed-tools",
    "Bash Edit Write Read Glob Grep WebFetch WebSearch Task",
)

# Every call inherits the user's Claude Code context, and the skill listing is the
# largest part of it — measured at 20.5k -> 7.8k tokens (cost $0.146 -> $0.078)
# on a config with 1822 installed skills. grill needs none of them: it sends one
# self-contained prompt and wants one JSON object back.
#
# `--bare` would cut more but is rejected: it reads auth strictly from
# ANTHROPIC_API_KEY, which breaks the "no second credential" property that the
# design's model-selection section is built on.
NO_SKILLS = ("--disable-slash-commands",)


class LLMError(RuntimeError):
    """The CLI failed, timed out, or returned something unparseable."""


@dataclass
class Completion:
    text: str
    cost_usd: float | None
    duration_ms: int | None


def complete(prompt: str, *, timeout: int = DEFAULT_TIMEOUT) -> Completion:
    """Run one non-interactive turn and return its text.

    Deliberately no `--model`: the user's selection is the quality bar, and it is
    theirs to set.
    """
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--strict-mcp-config",
        *NO_TOOLS,
        *NO_SKILLS,
    ]
    try:
        # stdin must be closed explicitly. The CLI waits on it and then warns,
        # and at a SessionEnd hook there is no terminal attached to supply it —
        # inheriting the parent's stdin turns a 3s call into a stall.
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LLMError("claude CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude timed out after {timeout}s") from exc

    if proc.returncode != 0:
        raise LLMError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:400]}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"unparseable envelope: {proc.stdout[:400]}") from exc

    if envelope.get("is_error"):
        raise LLMError(f"claude reported an error: {str(envelope.get('result'))[:400]}")

    result = envelope.get("result")
    if not isinstance(result, str):
        raise LLMError(f"no result text in envelope: {proc.stdout[:400]}")

    return Completion(
        text=result,
        cost_usd=envelope.get("total_cost_usd"),
        duration_ms=envelope.get("duration_ms"),
    )


# A bare JSON number: optional leading '-', digits, optional fraction/exponent.
# Used only to recover a key's value when it isn't quoted — `turn` in the
# moments schema is asked for as a bare integer, so this is the common shape
# for that field, not an edge case.
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def salvage_flat_object(text: str, keys: tuple[str, ...]) -> dict[str, Any]:
    """Recover a flat string/null/number object whose inner quotes were never escaped.

    Models routinely quote the user inside a JSON string field without escaping
    the quotes, which is not JSON and which no amount of prompting reliably
    prevents. Discarding those responses is not neutral: the field most likely to
    contain a quotation mark is `quote`, so a strict parser silently biases
    triage toward dropping exactly the sessions it should keep.

    Values are read to the last `"` before the next key or the closing brace,
    which is unambiguous for a flat object even when the value contains quotes.

    Recognizes exactly four unquoted shapes — `null`, `true`, `false`, and a
    JSON number — plus quoted strings. Anything else (an array, an object, or
    just malformed text) is left unrecovered: this stays a targeted recovery
    of known-safe shapes, not a general permissive parser.
    """
    result: dict[str, Any] = {}
    for key in keys:
        marker = f'"{key}"'
        at = text.find(marker)
        if at == -1:
            continue
        colon = text.find(":", at + len(marker))
        if colon == -1:
            continue
        rest = text[colon + 1 :].lstrip()

        if rest.startswith("null"):
            result[key] = None
            continue
        if rest.startswith(("true", "false")):
            result[key] = rest.startswith("true")
            continue
        if not rest.startswith('"'):
            number = _NUMBER.match(rest)
            if number:
                raw_num = number.group(0)
                result[key] = float(raw_num) if any(c in raw_num for c in ".eE") else int(raw_num)
            continue

        # Find where this value ends: the next `"key":` at the same level, or the
        # end of the object. The value's closing quote is the last one before it.
        offset = len(text) - len(rest)
        end = len(text)
        for other in keys:
            if other == key:
                continue
            found = text.find(f'"{other}"', offset + 1)
            if found != -1:
                end = min(end, found)
        closing = text.rfind('"', offset + 1, end)
        if closing > offset:
            value = text[offset + 1 : closing]
            # Trim a trailing comma/brace that crept in before the next key.
            result[key] = value.rstrip().rstrip(",").rstrip()
    return result


def _find_matching_close(text: str, start: int, open_char: str, close_char: str) -> int | None:
    """Index of the bracket that closes the one at `start`, or None if unmatched.

    Shared by the object and array scanners below: both need to walk past a
    quoted string without letting a bracket character inside it count as
    structure. Rendered turns are prefixed `[n]` and developer quotes routinely
    contain both `{}` and `[]`, so this has to be string-aware rather than a
    naive counter.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return i
    return None


def _split_top_level_objects(text: str) -> list[str]:
    """Every top-level `{...}` span in `text`, string-aware brace matching.

    Used to recover an array element by element when the array as a whole
    won't parse. Splitting has to tolerate the same unescaped-quote mess that
    broke the whole-array parse in the first place: an in-progress string
    incorrectly closed early by a stray `"` does not, in practice, contain a
    stray `{` or `}`, so the brace count stays trustworthy even though the
    string tracking briefly wasn't.
    """
    spans = []
    start = text.find("{")
    while start != -1:
        end = _find_matching_close(text, start, "{", "}")
        if end is None:
            break
        spans.append(text[start : end + 1])
        start = text.find("{", end + 1)
    return spans


def _salvage_array_elements(interior: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Recover an array element by element when the whole array won't parse.

    Mirrors `extract_json_object`'s reasoning at the granularity of one
    element: one unescaped quote inside a `quote` field must cost that
    element, not its siblings. Each element is tried as clean JSON first;
    only a genuinely broken element falls through to `salvage_flat_object`.
    Elements neither path can recover are dropped rather than failing the
    whole response — a partial list of moments is still useful, unlike a
    partially-recovered single verdict.
    """
    recovered: list[dict[str, Any]] = []
    for element in _split_top_level_objects(interior):
        try:
            parsed = json.loads(element)
        except json.JSONDecodeError:
            parsed = salvage_flat_object(element, keys)
        if isinstance(parsed, dict) and parsed:
            recovered.append(parsed)
    return recovered


def extract_json_object(text: str, salvage_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """Pull the first JSON object out of a model response.

    Models fence JSON, preface it, or both. Parsing the whole string first keeps
    the clean case exact and falls back to brace-matching rather than a regex,
    which nested objects defeat. `salvage_keys`, when given, enables a last-ditch
    flat-object recovery for unescaped inner quotes.
    """
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        end = _find_matching_close(text, start, "{", "}")
        if end is not None:
            try:
                candidate = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(candidate, dict):
                    return candidate
        start = text.find("{", start + 1)

    if salvage_keys:
        salvaged = salvage_flat_object(text, salvage_keys)
        if salvaged:
            return salvaged

    raise LLMError(f"no JSON object in response: {text[:400]}")


def extract_json_array(text: str, salvage_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Pull the first JSON array of objects out of a model response.

    The array counterpart of `extract_json_object`. Brace-matching rather than a
    regex, and string-aware: rendered turns are prefixed `[n]`, so quotes very
    often contain brackets that naive matching would treat as structure.

    Under the single-object response, one unescaped quote cost one verdict.
    Under the array shape it used to cost the whole session: one bad element
    made `json.loads` fail for the entire array, and the caller had no way to
    tell "the model found nothing" from "the model found six things and wrote
    one badly." `salvage_keys`, when given, recovers per element instead of
    giving up on the whole array — see `_salvage_array_elements`. Without
    `salvage_keys` the old strict behavior is unchanged: a malformed array is a
    failure, full stop.
    """
    text = text.strip()
    start = text.find("[")
    first_span: tuple[int, int] | None = None
    while start != -1:
        end = _find_matching_close(text, start, "[", "]")
        if end is not None:
            if first_span is None:
                first_span = (start, end)
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(parsed, list):
                    return [item for item in parsed if isinstance(item, dict)]
        start = text.find("[", start + 1)

    if salvage_keys and first_span is not None:
        start, end = first_span
        moments = _salvage_array_elements(text[start + 1 : end], salvage_keys)
        if moments:
            return moments

    raise LLMError(f"no JSON array in response: {text[:400]}")
