"""Suite-wide guardrails.

`GRASK_HOME` is redirected for every test, without exception. Storage and the
capture log both resolve their paths at call time through `grask_home()`, so any
test that reaches an error path writes to the developer's real
`~/.claude/grask/` unless something stops it — and the error paths are exactly
what this suite exercises most.

That is not hypothetical. Three tests here leaked into a real grask.log before
this fixture existed, including a fake "triage error: timeout after 300s" that
looked, in the real log, exactly like a genuine production failure. Redirecting
per-test is the fix that works by default rather than by remembering.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import grask.llm


@pytest.fixture(autouse=True)
def isolated_grask_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point GRASK_HOME at a temp dir for the duration of every test.

    Tests that assert on the log can still take `tmp_path` and read
    `tmp_path / "grask.log"`: this sets GRASK_HOME to the same `tmp_path` they
    get, so an explicit `monkeypatch.setenv` in a test is a harmless no-op rather
    than a conflict.
    """
    monkeypatch.setenv("GRASK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to shell out to the real CLI from a test.

    Every stage is injectable and every test injects one, so nothing here should
    ever reach `llm.complete`. What makes that assumption rot is a *new* stage:
    the tests that predate it keep passing while quietly gaining a real call,
    because they inject the stages they knew about and take the default for the
    one they did not. That is not theoretical — it is what adding stage 4 did to
    eight tests in this suite, at ~6s and real money each.

    It refuses the `claude` binary specifically rather than every subprocess.
    Two other launches in this suite are legitimate — `install.py` probes a
    python version, and `capture_run.py` spawns the detached worker — and a
    guard that also stopped those would have to be remembered around, which is
    the property this file exists to avoid needing.

    Both `run` and `Popen` are wrapped. `llm.py` uses `run` today, but the
    author this guard exists for is writing a stage that does not exist yet, and
    a guard they can escape by reaching for the other spelling is a guard that
    holds only until someone does. Everything else in `subprocess`
    (`check_output`, `call`, `check_call`) goes through `run`, so those two
    cover the module.
    """
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def guard(real):
        def refuse(argv, *args, **kwargs):
            if argv and str(argv[0]).endswith("claude"):
                # `pytest.fail` and not `raise AssertionError`: an AssertionError
                # is an ordinary Exception, and `capture_session` catches
                # Exception on purpose — it runs detached and turns every
                # failure into a row. It would catch this one too, and the
                # author of the next stage would see an `error` verdict rather
                # than a message naming the problem. `Failed` derives from
                # BaseException, so it passes straight through.
                pytest.fail(
                    "a test tried to make a real model call — inject the stage instead "
                    "(capture_session takes triage=, seed=, probe=, verify=)",
                    pytrace=False,
                )
            return real(argv, *args, **kwargs)

        return refuse

    monkeypatch.setattr(grask.llm.subprocess, "run", guard(real_run))
    monkeypatch.setattr(grask.llm.subprocess, "Popen", guard(real_popen))
