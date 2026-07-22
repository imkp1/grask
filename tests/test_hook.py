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

from grill.hook import main


def stdin(payload: object) -> io.StringIO:
    return io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))


def test_spawns_the_worker_with_the_transcript_path():
    spawned = []
    code = main(
        stdin=stdin(
            {
                "session_id": "0198e4f1",
                "transcript_path": "/p/0198e4f1.jsonl",
                "hook_event_name": "SessionEnd",
            }
        ),
        spawn=spawned.append,
    )
    assert code == 0
    assert spawned == ["/p/0198e4f1.jsonl"]


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


def test_a_failing_spawn_still_exits_zero():
    def boom(path):
        raise OSError("fork failed")

    assert main(stdin=stdin({"transcript_path": "/p/0198e4f1.jsonl"}), spawn=boom) == 0


def test_spawn_argv_is_the_running_interpreter_and_the_capture_module(monkeypatch, tmp_path):
    import subprocess
    import sys

    from grill import hook

    # spawn() opens the real log file, so redirect GRILL_HOME or this test
    # scribbles in the developer's actual ~/.claude/grill/.
    monkeypatch.setenv("GRILL_HOME", str(tmp_path))
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    hook.spawn("/p/0198e4f1.jsonl")

    assert seen["argv"] == [sys.executable, "-m", "grill.capture", "/p/0198e4f1.jsonl"]
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
