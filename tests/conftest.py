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

from pathlib import Path

import pytest


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
