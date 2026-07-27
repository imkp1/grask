"""Tests for the SessionEnd hook.

The worker is never actually spawned here; `spawn` is injected. What these pin
down is that the hook is quiet and fast: it parses stdin, hands off one path, and
returns 0 no matter what it was given. A hook that errors on the way out of a
session is worse than one that does nothing, because the developer sees it and
has no idea what it was for.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from grask.hook import main


def stdin(payload: object) -> io.StringIO:
    return io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    """A transcript that is actually on disk.

    The hook now checks, so a fabricated path no longer exercises the spawn
    path — it exercises the drop path, and would have quietly turned every test
    below into an assertion about nothing.
    """
    path = tmp_path / "0198e4f1.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_spawns_the_worker_with_the_transcript_path(transcript: Path):
    spawned = []
    code = main(
        stdin=stdin(
            {
                "session_id": "0198e4f1",
                "transcript_path": str(transcript),
                "hook_event_name": "SessionEnd",
            }
        ),
        spawn=spawned.append,
    )
    assert code == 0
    assert spawned == [str(transcript)]


def test_a_transcript_that_is_not_on_disk_is_dropped():
    """SessionEnd fires for grask's own `claude -p` stages too, and those run
    with `--no-session-persistence` — the path in their payload never becomes a
    file. Spawning for one costs three processes and an `error` row per capture
    describing nothing that went wrong.
    """
    spawned = []

    code = main(
        stdin=stdin({"transcript_path": "/p/never-written.jsonl"}), spawn=spawned.append
    )

    assert code == 0
    assert spawned == []


def test_malformed_stdin_exits_zero_without_spawning():
    spawned = []
    assert main(stdin=stdin("not json at all"), spawn=spawned.append) == 0
    assert spawned == []


def test_empty_stdin_exits_zero_without_spawning():
    spawned = []
    assert main(stdin=io.StringIO(""), spawn=spawned.append) == 0
    assert spawned == []


def test_missing_transcript_path_exits_zero_without_spawning():
    spawned = []
    assert main(stdin=stdin({"session_id": "0198e4f1"}), spawn=spawned.append) == 0
    assert spawned == []


def test_a_failing_spawn_still_exits_zero(transcript: Path):
    def boom(path):
        raise OSError("fork failed")

    assert main(stdin=stdin({"transcript_path": str(transcript)}), spawn=boom) == 0


def test_spawn_argv_is_the_running_interpreter_and_the_capture_module(monkeypatch, tmp_path):
    import subprocess
    import sys

    from grask import hook

    # spawn() opens the real log file, so redirect GRASK_HOME or this test
    # scribbles in the developer's actual ~/.claude/grask/.
    monkeypatch.setenv("GRASK_HOME", str(tmp_path))
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    hook.spawn("/p/0198e4f1.jsonl")

    assert seen["argv"] == [sys.executable, "-m", "grask.capture", "/p/0198e4f1.jsonl"]
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
