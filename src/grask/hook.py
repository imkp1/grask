"""The SessionEnd entry point.

Reads the hook payload from stdin, starts the capture worker detached, and
returns immediately. The parent is gone long before the first model call, so
whether the harness honours an async hook does not matter — this is non-blocking
by construction rather than by permission.

Everything is swallowed. The developer is on their way out of a session; a hook
that raises at that moment produces a scary message about a tool they cannot see
and did not ask about.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import TextIO

from grask.capture import log
from grask.storage import grask_home


def spawn(transcript_path: str) -> None:
    """Start the capture worker and forget about it.

    `start_new_session` (setsid) detaches it from this process group, so it
    survives the session ending. stdin is closed and both output streams go to
    the log — a detached process writing to an inherited terminal is a process
    that scribbles on the next thing the developer does.
    """
    path = grask_home() / "grask.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-m", "grask.capture", transcript_path],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=handle,
    )


def main(stdin: TextIO | None = None, spawn: Callable[[str], None] = spawn) -> int:
    """Parse the SessionEnd payload and hand off. Always returns 0."""
    stream = sys.stdin if stdin is None else stdin
    try:
        payload = json.loads(stream.read() or "{}")
        transcript_path = payload.get("transcript_path") if isinstance(payload, dict) else None
        if isinstance(transcript_path, str) and transcript_path:
            spawn(transcript_path)
    except Exception as exc:
        log(f"hook ignored a payload it could not use: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
