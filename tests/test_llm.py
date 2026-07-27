"""Tests for the CLI adapter — specifically, the flags and the way they can fail.

No `claude` is spawned here; `subprocess.run` is replaced. What these pin down
is the part that has teeth: `complete` now passes flags an older CLI may not
recognise, and it runs from a detached SessionEnd worker where a non-zero exit
reaches nobody. A flag chosen to save tokens must not be able to stop capture
altogether and do it silently.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from grask import llm
from grask.llm import LLMError, build_argv, complete


def envelope(text: str = '{"ok": true}') -> str:
    return json.dumps({"result": text, "total_cost_usd": 0.01, "duration_ms": 900})


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout or envelope(), stderr=stderr
    )


@pytest.fixture(autouse=True)
def undegraded(monkeypatch: pytest.MonkeyPatch):
    """`_degraded` is process-global by design — one probe, then remembered.
    That makes it leak between tests unless it is reset per test."""
    monkeypatch.setattr(llm, "_degraded", False)


class TestArgv:
    def test_no_model_is_ever_named(self):
        """The user's selection is the quality bar, and it is theirs to set."""
        assert "--model" not in build_argv("hello")

    def test_tools_are_withheld_rather_than_forbidden(self):
        """`--disallowed-tools` still tells the model the tools exist. Measured
        at 8,918 -> 5,585 input tokens, $0.0048 -> $0.0031, for definitions
        nothing may use. Tokens, not seconds — see the note in llm.py."""
        argv = build_argv("hello")

        assert "--tools" in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert "--disallowed-tools" not in argv

    def test_calls_do_not_persist_a_session(self):
        """Each `-p` call is a session, and a persisted session is a transcript
        the SessionEnd hook then captures — 279 rows of grask reading itself."""
        assert "--no-session-persistence" in build_argv("hello")

    def test_the_degraded_form_drops_only_the_optional_flags(self):
        argv = build_argv("hello", degraded=True)

        assert "--tools" not in argv
        assert "--no-session-persistence" not in argv
        assert "--disallowed-tools" in argv
        # The output contract is what everything downstream reads, and it is
        # identical in both forms.
        assert argv[argv.index("--output-format") + 1] == "json"


class TestFlagCompatibility:
    def calls(self, monkeypatch: pytest.MonkeyPatch, *results):
        seen = []
        remaining = list(results)

        def fake_run(argv, **kwargs):
            seen.append(argv)
            return remaining.pop(0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        return seen

    def test_an_unknown_flag_retries_once_without_it(self, monkeypatch):
        seen = self.calls(
            monkeypatch,
            completed(returncode=1, stderr="error: unknown option '--tools'"),
            completed(),
        )

        result = complete("hello")

        assert result.text == '{"ok": true}'
        assert len(seen) == 2
        assert "--tools" in seen[0] and "--tools" not in seen[1]

    def test_the_demotion_is_remembered_for_the_process(self, monkeypatch):
        """Three stages per capture. Re-probing a flag the CLI already rejected
        would pay the failed call three times over."""
        seen = self.calls(
            monkeypatch,
            completed(returncode=1, stderr="error: unknown option '--tools'"),
            completed(),
            completed(),
        )

        complete("first")
        complete("second")

        assert len(seen) == 3, "the second call must not re-probe the flag"
        assert "--tools" not in seen[2]

    def test_a_real_failure_is_not_retried_as_a_flag_problem(self, monkeypatch):
        """Otherwise every genuine outage costs two calls instead of one, and
        the error the developer eventually sees is from the wrong invocation."""
        seen = self.calls(
            monkeypatch, completed(returncode=1, stderr="Credit balance is too low")
        )

        with pytest.raises(LLMError, match="Credit balance"):
            complete("hello")

        assert len(seen) == 1

    def test_a_degraded_call_that_still_fails_reports_the_failure(self, monkeypatch):
        self.calls(
            monkeypatch,
            completed(returncode=1, stderr="error: unknown option '--tools'"),
            completed(returncode=1, stderr="something else entirely"),
        )

        with pytest.raises(LLMError, match="something else entirely"):
            complete("hello")
