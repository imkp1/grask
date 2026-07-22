"""Seed a throwaway grask database with one probe, for recording the demo.

Writes to $GRASK_HOME, never to the real one. The probe is the worked example
from docs/design.md ("What the developer sees"), so the GIF and the design doc
show the same question.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from grask.probe import Probe, Rubric  # noqa: E402
from grask.seed import Seed  # noqa: E402
from grask.storage import Store  # noqa: E402

home = os.environ.get("GRASK_HOME")
if not home:
    sys.exit("refusing to run without GRASK_HOME set: this would write to the real db")

store = Store(Path(home) / "grask.db")
store.record_session(
    session_id="demo-session",
    transcript_path="/dev/null",
    cwd=str(Path.cwd()),
    git_branch="main",
    verdict="ask",
    signal="asked_why",
    topic="retry backoff in the webhook dispatcher",
)
seed_id = store.add_seed(
    Seed(
        session_id="demo-session",
        turn=14,
        signal="asked_why",
        topic="retry backoff in the webhook dispatcher",
        quotes=("why do we need jitter here?",),
        refs=("dispatcher.py:88",),
        decision="added exponential backoff with random jitter to the retry loop",
        hypothesis=(
            "The developer accepted that backoff spreads out retries without "
            "knowing that backoff alone leaves clients synchronised."
        ),
    )
)
store.add_probe(
    seed_id,
    Probe(
        question=(
            "Your retry loop sleeps 2**attempt seconds between attempts. Why does "
            "adding random jitter matter more as the number of clients grows?"
        ),
        options=(
            "Jitter reduces the total number of retries each client makes.",
            "Clients knocked out together retry together; jitter spreads them back out.",
            "Exponential backoff overflows without a random term to bound it.",
            "Jitter is what makes the sleep interruptible by a signal.",
        ),
        correct_idx=1,
        explanation=(
            "Backoff decides how long each client waits. It does nothing about them all "
            "waiting the same amount. Clients dropped by one outage come back in lockstep, "
            "so the recovering service takes the same thundering herd on every cycle. "
            "Jitter decorrelates the schedules."
        ),
        rubric=Rubric(
            topic="retry backoff in the webhook dispatcher",
            hypothesis=(
                "The developer accepted that backoff spreads out retries without "
                "knowing that backoff alone leaves clients synchronised."
            ),
        ),
    ),
)
print("seeded")
