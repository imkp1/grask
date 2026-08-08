"""Where capture puts what it found.

One SQLite file under `~/.claude/grask/`. Five tables: one per capture stage
that produces something worth keeping, and two that record what was asked and
how it graded. Databases from the free-text era may also carry a
`criterion_results` table; nothing writes or reads it anymore, and fresh
databases never grow it.

Silence and failure are recorded, not just keeps. That is deliberate: keep-rate
and failure-rate are the two numbers that say whether any of this is working, and
a table you have to remember to populate is a table that lies. It is also what
makes capture idempotent — a session_id already present means we have seen it,
whatever we concluded.

Idempotent, and singly-owned: `begin_session` returns whether the caller won the
session, because "already present" is a race when two workers ask at once and
only the INSERT is atomic. `record_session` refusing to overwrite a terminal
verdict is not enough on its own — `seeds` and `probes` have no such refusal, so
a loser that kept running wrote a second question about the same session.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from grask.ask import Interrogation, PendingProbe
from grask.probe import Probe, Rubric
from grask.seed import Seed

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    transcript_path TEXT NOT NULL,
    cwd             TEXT,
    git_branch      TEXT,
    verdict         TEXT NOT NULL,
    signal          TEXT,
    topic           TEXT,
    cost_usd        REAL,
    duration_ms     INTEGER,
    discarded_usd   REAL,
    discard_reason  TEXT,
    triaged_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seeds (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    turn        INTEGER NOT NULL,
    signal      TEXT NOT NULL,
    topic       TEXT NOT NULL,
    quotes      TEXT NOT NULL,
    refs        TEXT NOT NULL,
    decision    TEXT NOT NULL,
    hypothesis  TEXT NOT NULL,
    cost_usd    REAL,
    duration_ms INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probes (
    id          INTEGER PRIMARY KEY,
    seed_id     INTEGER NOT NULL REFERENCES seeds(id),
    question    TEXT NOT NULL,
    criteria    TEXT NOT NULL,
    options     TEXT,
    correct_idx INTEGER,
    explanation TEXT,
    cost_usd    REAL,
    duration_ms INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asks (
    id           INTEGER PRIMARY KEY,
    probe_id     INTEGER NOT NULL UNIQUE REFERENCES probes(id),
    asked_at     TEXT NOT NULL,
    confidence   INTEGER,
    outcome      TEXT NOT NULL,
    objection    TEXT,
    turns        INTEGER NOT NULL,
    cost_usd     REAL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS answers (
    id         INTEGER PRIMARY KEY,
    ask_id     INTEGER NOT NULL REFERENCES asks(id),
    turn       INTEGER NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

"""

# Columns added to tables that had already shipped, per table. `_migrate` walks
# this; `SCHEMA` above carries the same columns for databases created fresh.
ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "sessions": (
        ("duration_ms", "INTEGER"),
        ("discarded_usd", "REAL"),
        ("discard_reason", "TEXT"),
    ),
    "seeds": (("duration_ms", "INTEGER"),),
    "probes": (
        ("options", "TEXT"),
        ("correct_idx", "INTEGER"),
        ("explanation", "TEXT"),
        ("duration_ms", "INTEGER"),
    ),
}

# A probe about work you did last week is a quiz, not a question. Seven days is
# the outer edge of "you still remember writing this". Expiry is computed at
# query time rather than stored, so nothing has to sweep and no lifecycle column
# can fall out of sync with the clock.
PROBE_TTL_DAYS = 7

# The six ways `next_probe` can come back with nothing. See `empty_reason`.
EmptyReason = Literal[
    "over_cap", "capturing", "unverified", "expired", "caught_up", "never"
]

# The verdict a session carries while its capture is still running. Not a
# triage outcome — `capture_session` writes it before the first model call and
# overwrites it with the real verdict at the end. It exists so the ~45s the
# pipeline spends in four sequential model calls is a state the queue can
# name, rather than a window in which a pending probe is indistinguishable from
# no probe at all.
CAPTURING = "capturing"

# How long a `capturing` row is believed. The pipeline's worst case is triage
# (180s) plus seed (180s) plus probe's three attempts (540s) plus verify's three
# (540s) = 24 minutes, so anything older than this is a worker that died without
# writing its verdict — a crash, a reboot, a `kill`. Past the window the row
# stops claiming a probe is coming and stops blocking a re-capture of the same
# transcript, because a marker with no way out is how a queue jams permanently.
CAPTURE_STALE_MINUTES = 30

# How long a write waits for another connection's lock before giving up. The
# writers are detached capture workers, one per ended session, and they overlap
# whenever two sessions end close together — the pipeline holds each of them for
# ~45s, so "two at once" is ordinary rather than rare.
#
# Well above sqlite3's 5s default, because the cost of the two directions is not
# symmetric. Waiting longer costs a background worker some idle seconds nobody
# sees; giving up early costs an uncaught OperationalError, which for `grask
# serve` is a traceback in front of the developer and for a worker is a captured
# session silently lost. The actual writes are milliseconds — this budget is for
# queueing behind someone else's, never for our own.
BUSY_TIMEOUT_SECONDS = 30.0

# The verdict a session carries when stage 4 read the question grask wrote for
# it and would not vouch for the answer key. A terminal verdict like `silent`
# and `error`, and deliberately neither of them: the session was worth asking
# about (triage said `ask`, and the seed is stored) and nothing malfunctioned.
# It shares `PROBE_TTL_DAYS` rather than the capture window because it is not a
# transient state — it is a fact about a session, and it stops being worth
# mentioning at the same age as the probes it would have competed with. It also
# stops being worth mentioning the moment a *later* session mints a probe: see
# `empty_reason`, where the age limit alone was not enough.
UNVERIFIED = "unverified"

# The verdict a session carries when triage kept it and stage 2 disagreed —
# nothing here supports a specific misconception, so no question was written.
# Terminal, like `silent` and `unverified`.
#
# Deliberately neither of the two it sits between. Not `error`: nothing
# malfunctioned, and the error rate is the number that says whether the prompt
# is working, so a working decline inside it is a lie about the pipeline. Not
# `silent`: triage did keep this session, and the count of sessions stage 2
# talked triage out of is the only way to see the decline collapsing yield
# rather than trimming it.
#
# Unlike `unverified` it is invisible to `empty_reason`, and that is the whole
# difference. `unverified` tells the developer a question existed and was thrown
# away; a decline means no question was ever written, which from the queue's
# side is exactly what silence looks like. There is nothing to explain.
DECLINED = "declined"


def _pending_from_row(row: sqlite3.Row) -> PendingProbe:
    """One probes-join-seeds row, parsed defensively rather than trusted.

    A row that fails to parse is returned anyway and becomes ask's `error`
    outcome, which is the one error the design keeps.
    """
    try:
        loaded = json.loads(row["options"])
        options = (
            tuple(o for o in loaded if isinstance(o, str))
            if isinstance(loaded, list)
            else ()
        )
    except (TypeError, ValueError):
        options = ()

    correct_idx = row["correct_idx"]
    return PendingProbe(
        probe_id=int(row["id"]),
        question=row["question"],
        options=options,
        correct_idx=int(correct_idx) if isinstance(correct_idx, int) else None,
        explanation=row["explanation"] or "",
        rubric=Rubric(topic=row["topic"], hypothesis=row["hypothesis"]),
        created_at=row["created_at"],
    )


def _json_list(raw: str | None) -> list[str]:
    """A stored JSON array of strings, or nothing.

    Same defensiveness as `_pending_from_row`: a seed whose quotes will not
    parse is still a seed worth re-asking, and stage 3 tolerates an empty list
    where it would not tolerate a crash on the way in.
    """
    try:
        loaded = json.loads(raw or "")
    except (TypeError, ValueError):
        return []
    return [item for item in loaded if isinstance(item, str)] if isinstance(loaded, list) else []


@dataclass(frozen=True)
class AnsweredProbe:
    """One probe the developer actually answered, for the history view."""

    asked_at: str
    outcome: str
    topic: str
    question: str


@dataclass(frozen=True)
class Stats:
    """What grask has done for this developer, as they would count it.

    Deliberately not a score. `passed` and `failed` are here because a developer
    who answers questions is owed the record of them, but design.md's rule that
    one probe cannot identify understanding means nothing may present them as a
    grade — `survey.py` and the batch runners already cover the pipeline
    numbers, and this is the only view built for the person being asked.

    `raised` counts probes minted, which is not `answered` plus `pending`: a
    probe can expire unasked, and one recorded as an `error` was consumed
    without ever being a question.
    """

    sessions: int
    raised: int
    answered: int
    passed: int
    failed: int
    skipped: int
    pending: int
    recent: tuple[AnsweredProbe, ...]


@dataclass(frozen=True)
class UnprobedSeed:
    """A stored seed with no question written from it, and where to find its dialogue."""

    seed_id: int
    seed: Seed
    transcript_path: Path


def grask_home() -> Path:
    """Where the db and the log live. Env-overridable so tests never touch the real one."""
    override = os.environ.get("GRASK_HOME")
    return Path(override) if override else Path.home() / ".claude" / "grask"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inserted_id(cursor: sqlite3.Cursor) -> int:
    """The rowid sqlite just assigned.

    `lastrowid` is typed `int | None` because it is None on a cursor that has not
    inserted anything. Every call site here runs immediately after an INSERT, so
    a None means the driver broke its own contract — worth raising on rather than
    coercing, since the value becomes a foreign key the next write depends on.
    """
    if cursor.lastrowid is None:  # pragma: no cover - driver contract violation
        raise sqlite3.DatabaseError("INSERT produced no rowid")
    return cursor.lastrowid


class Store:
    """The capture database.

    Opens and migrates on construction; `CREATE TABLE IF NOT EXISTS` means that
    is safe to do on every hook firing, which is exactly how often it happens.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or grask_home() / "grask.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        self.conn.row_factory = sqlite3.Row
        # Off by default in sqlite, and the one thing keeping an orphaned seed
        # from outliving the session that explains it.
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._set_journal_mode()
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _set_journal_mode(self) -> None:
        """Ask for WAL, and carry on with whatever sqlite grants.

        The default rollback journal takes an exclusive lock for the whole of a
        write, so a capture worker's commit blocks `grask serve` from *reading*.
        WAL lets the read proceed against the pre-write snapshot, which is the
        difference between a 60ms `serve` and one that queues behind a
        background worker the developer cannot see.

        Not asserted, because WAL needs shared memory the filesystem may not
        provide — a `GRASK_HOME` on NFS or some network mounts refuses it. There
        the old journal mode is still correct, only less concurrent, and
        `BUSY_TIMEOUT_SECONDS` is what carries it. A database that will not take
        WAL is not a database grask should decline to open.
        """
        # pragma: no cover on the suppression - filesystem dependent
        with contextlib.suppress(sqlite3.DatabaseError):
            self.conn.execute("PRAGMA journal_mode = WAL")

    def _migrate(self) -> None:
        """Bring an older database up to the current schema, one column at a time.

        `CREATE TABLE IF NOT EXISTS` cannot add columns to a table that already
        exists, so every column added after a table shipped needs an ALTER here.
        Every one is nullable: a legacy row genuinely has no value for it, and
        writing a default would invent a measurement nobody took.

        Adding a column means adding it to `ADDED_COLUMNS` as well as to
        `SCHEMA` — fresh databases get it from the first, existing ones from the
        second, and a column in only one of the two produces a database whose
        shape depends on when it was created.
        """
        for table, columns in ADDED_COLUMNS.items():
            existing = {
                row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for name, kind in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _capture_cutoff(self) -> str:
        return (
            datetime.now(timezone.utc) - timedelta(minutes=CAPTURE_STALE_MINUTES)
        ).isoformat()

    def _ttl_cutoff(self) -> str:
        """The timestamp a probe must be newer than to still be worth asking.

        Four queries need this — `next_probe`, `empty_reason`, `unprobed_seeds`,
        `stats` — and each had spelled it out. A probe the queue serves but the
        count calls expired is two answers to one question, and four copies of
        an expression is four chances to get one of them wrong.
        """
        return (datetime.now(timezone.utc) - timedelta(days=PROBE_TTL_DAYS)).isoformat()

    def has_session(self, session_id: str) -> bool:
        """Whether this session is already accounted for, and must not be re-captured.

        A fresh `capturing` row counts: a hook that fires twice for one session
        must not pay for it twice, and the second worker would race the first.
        A stale one does not, which is the whole of the recovery path — a worker
        killed mid-flight leaves a row that would otherwise block its session
        forever while promising a probe that is never coming.
        """
        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = :id"
            "  AND (verdict != :capturing OR triaged_at >= :cutoff)",
            {
                "id": session_id,
                "capturing": CAPTURING,
                "cutoff": self._capture_cutoff(),
            },
        ).fetchone()
        return row is not None

    def begin_session(self, *, session_id: str, transcript_path: str) -> bool:
        """Claim a session for capture, before the first model call.

        Returns whether this caller owns it. **A False means stop**: someone
        else is already capturing this session, or already finished it.

        Deliberately carries none of triage's fields: nothing is known yet, and
        a row that guessed would be a row `record_session` has to correct.
        `triaged_at` is the start time here and the finish time on the row that
        replaces it — a marker nobody can read for more than
        `CAPTURE_STALE_MINUTES` does not need two columns to say when it was
        written.

        The return value is the whole of the concurrency control, and it is why
        the conflict clause tests staleness rather than just `capturing`.
        `has_session` and this are two statements, so two workers can both pass
        the first one; the INSERT is where that has to be settled, because it is
        the only step sqlite makes atomic for us. Four outcomes, and the caller
        needs to tell them apart:

        - no row: inserted, we own it.
        - a `capturing` row past `CAPTURE_STALE_MINUTES`: the previous worker
          died, we take it over. This is the recovery path `has_session`
          already opens, and refusing here would jam the session forever.
        - a `capturing` row still inside the window: a live worker has it. Lose.
        - any terminal verdict: it is done. Lose.

        Only the first two update a row, so `rowcount` says which pair we are
        in. Without this, both workers ran the whole four-call pipeline and both
        wrote — `record_session` no-ops for the loser, but `add_seed` and
        `add_probe` do not, so the developer was asked one question twice and
        billed twice for it.
        """
        cursor = self.conn.execute(
            "INSERT INTO sessions"
            " (session_id, transcript_path, cwd, git_branch, verdict, triaged_at)"
            " VALUES (:id, :path, NULL, NULL, :capturing, :now)"
            " ON CONFLICT(session_id) DO UPDATE SET triaged_at = :now"
            "  WHERE sessions.verdict = :capturing AND sessions.triaged_at < :cutoff",
            {
                "id": session_id,
                "path": transcript_path,
                "capturing": CAPTURING,
                "cutoff": self._capture_cutoff(),
                "now": _now(),
            },
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def record_session(
        self,
        *,
        session_id: str,
        transcript_path: str,
        cwd: str | None,
        git_branch: str | None,
        verdict: str,
        signal: str | None = None,
        topic: str | None = None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        discarded_usd: float | None = None,
        discard_reason: str | None = None,
    ) -> None:
        """Record one triaged session's outcome.

        A second call for the same id does nothing — *unless* the row still says
        `capturing`, which is this session's own marker from `begin_session` and
        the one row a verdict is allowed to replace. Terminal verdicts stay
        immutable: that is what stands between a re-fired hook and paying for
        the same session twice.

        `cost_usd` and `duration_ms` are triage's alone. Seed and probe carry
        their own, because a single column that means different things per
        verdict is a column nobody can sum.

        Both are None on the stage-0 path, which records a silent session
        without ever calling a model.

        `discarded_usd` is the one spend with nowhere else to live: what stages
        3 and 4 cost on a question stage 4 then threw away. There is no probes
        row to carry it, so without a column it is not recorded at all.

        Its own column rather than folded into `cost_usd`, for one reason:
        `SUM(discarded_usd)` is what stage 4 has cost to produce nothing, and
        that is the number that decides whether the stage is kept, tuned, or
        reverted. Merged, it is unrecoverable — separating it back out would
        mean subtracting a per-session triage cost that no longer exists
        anywhere. It is a column because one specific question needs it exact,
        not because the two numbers are conceptually different kinds of money.

        `discard_reason` is why a session that had something to ask about
        produced no question: what stage 4 said when it threw the question away,
        or what stage 2 said it could not find when it declined to write a seed.
        Both used to reach only `grask.log`, which is not a channel anything can
        read: rotated at 1 MB and written by a detached worker. Nothing consumes
        it in code — feeding it back into stage 3 was tried and reverted, for
        want of any evidence that a re-run needs it. It is a column because the
        judgment is the only record of *why* a session produced no question:
        filtered to `unverified` it is how the locality rate gets measured at
        all, and filtered to `declined` it is the only way to tell stage 2
        declining correctly from stage 2 declining everything.

        The verdict alone does not scope either query. A discard that `reprobe`
        later redeemed keeps both its `unverified` verdict and its reason —
        nothing clears them — so a rate counted off the verdict counts the
        recoveries too. Excluding them is the `LEFT JOIN probes p ... WHERE p.id
        IS NULL` that `unprobed_seeds` already spells out.
        """
        self.conn.execute(
            "INSERT INTO sessions"
            " (session_id, transcript_path, cwd, git_branch, verdict, signal, topic,"
            "  cost_usd, duration_ms, discarded_usd, discard_reason, triaged_at)"
            " VALUES (:id, :path, :cwd, :branch, :verdict, :signal, :topic,"
            "         :cost, :duration, :discarded, :reason, :now)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            "  transcript_path = :path, cwd = :cwd, git_branch = :branch,"
            "  verdict = :verdict, signal = :signal, topic = :topic,"
            "  cost_usd = :cost, duration_ms = :duration,"
            "  discarded_usd = :discarded, discard_reason = :reason, triaged_at = :now"
            " WHERE sessions.verdict = :capturing",
            {
                "id": session_id,
                "path": transcript_path,
                "cwd": cwd,
                "branch": git_branch,
                "reason": discard_reason,
                "verdict": verdict,
                "signal": signal,
                "topic": topic,
                "cost": cost_usd,
                "duration": duration_ms,
                "discarded": discarded_usd,
                "capturing": CAPTURING,
                "now": _now(),
            },
        )
        self.conn.commit()

    def add_seed(self, seed: Seed) -> int:
        cursor = self.conn.execute(
            "INSERT INTO seeds"
            " (session_id, turn, signal, topic, quotes, refs, decision, hypothesis,"
            "  cost_usd, duration_ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seed.session_id,
                seed.turn,
                seed.signal,
                seed.topic,
                json.dumps(list(seed.quotes)),
                json.dumps(list(seed.refs)),
                seed.decision,
                seed.hypothesis,
                seed.cost_usd,
                seed.duration_ms,
                _now(),
            ),
        )
        self.conn.commit()
        return _inserted_id(cursor)

    def next_probe(self, *, max_options: int | None = None) -> PendingProbe | None:
        """The newest unasked, unexpired probe. One per invocation.

        Newest rather than oldest because the value is a question about the code
        you shipped this afternoon; oldest-first leads with the session you have
        most thoroughly forgotten. One at a time because the product is one
        question, not a queue.

        The rubric is reassembled from the seed's topic and hypothesis —
        `add_probe` deliberately does not duplicate them, so this join is where
        they come back together.

        `max_options` caps how many options a servable row may carry, for
        delivery surfaces with a hard UI limit. Over-cap rows are skipped and
        left pending — another surface may still serve them — while rows whose
        options are not valid JSON pass the filter deliberately: they must be
        served so the caller can record the `error` they are.
        """
        cutoff = self._ttl_cutoff()
        row = self.conn.execute(
            "SELECT p.id, p.question, p.options, p.correct_idx, p.explanation,"
            " p.created_at, s.topic, s.hypothesis"
            " FROM probes p"
            " JOIN seeds s ON s.id = p.seed_id"
            " LEFT JOIN asks a ON a.probe_id = p.id"
            " WHERE a.id IS NULL AND p.created_at >= :cutoff"
            " AND p.options IS NOT NULL"
            " AND (:cap IS NULL OR json_valid(p.options) = 0"
            "      OR json_array_length(p.options) <= :cap)"
            " ORDER BY p.created_at DESC, p.id DESC"
            " LIMIT 1",
            {"cutoff": cutoff, "cap": max_options},
        ).fetchone()

        return None if row is None else _pending_from_row(row)

    def empty_reason(self, *, max_options: int | None = None) -> EmptyReason:
        """Why `next_probe` came back empty, for callers that must explain it.

        Only meaningful once `next_probe` has returned None — this does not
        re-check that, it just accounts for the six ways a queue can look empty:

        - `over_cap`: servable rows exist but carry more options than
          `max_options`. First in precedence because it is the only reason with
          an action attached — another surface (the terminal) can still ask them,
          so a caller that reports "nothing queued" here contradicts itself.
        - `capturing`: a session ended within the last `CAPTURE_STALE_MINUTES`
          and its pipeline has not finished writing. Second because it is the
          only other reason that is temporary: the queue is not empty so much as
          not yet full, and every other note here would tell the developer to go
          end another session — the one action that does not help.
        - `unverified`: a session produced a question, stage 4 discarded it, and
          nothing has minted a probe since. Third for the same reason
          `capturing` is second — it is a queue that is empty for a reason
          particular to this session, and every note below it would tell the
          developer to go end another session, which here is in fact the right
          advice but only once they know the last one is not still coming.

          "Nothing since" is load-bearing, not belt-and-braces. Age alone left
          one discard outranking `caught_up` for the whole seven days: a
          developer who then earned a probe and answered it was still told the
          queue was empty because a question had been thrown away — about a
          session that was no longer the last one, every day for a week.
        - `expired`: probes were raised and went unasked past `PROBE_TTL_DAYS`.
        - `caught_up`: probes exist and none is still waiting.
        - `never`: no probe has ever been written.

        A row invisible to `next_probe` for some other reason (options stored as
        NULL) counts as `caught_up`: probes exist, none can be served, and
        claiming nothing was ever captured would be the falser of the two.
        """
        cutoff = self._ttl_cutoff()
        unasked = "FROM probes p LEFT JOIN asks a ON a.probe_id = p.id WHERE a.id IS NULL"
        row = self.conn.execute(
            "SELECT"
            "  (SELECT COUNT(*) FROM probes) AS total,"
            f"  (SELECT COUNT(*) {unasked}"
            "     AND p.created_at >= :cutoff AND p.options IS NOT NULL"
            "     AND :cap IS NOT NULL AND json_valid(p.options) = 1"
            "     AND json_array_length(p.options) > :cap) AS over_cap,"
            "  (SELECT COUNT(*) FROM sessions WHERE verdict = :capturing"
            "     AND triaged_at >= :stale) AS capturing,"
            "  (SELECT COUNT(*) FROM sessions WHERE verdict = :unverified"
            "     AND triaged_at >= :cutoff"
            "     AND triaged_at >="
            "       (SELECT COALESCE(MAX(created_at), '') FROM probes)) AS unverified,"
            f"  (SELECT COUNT(*) {unasked} AND p.created_at < :cutoff) AS expired",
            {
                "cutoff": cutoff,
                "cap": max_options,
                "capturing": CAPTURING,
                "unverified": UNVERIFIED,
                "stale": self._capture_cutoff(),
            },
        ).fetchone()

        if row["over_cap"]:
            return "over_cap"
        if row["capturing"]:
            return "capturing"
        if row["unverified"]:
            return "unverified"
        if row["expired"]:
            return "expired"
        return "caught_up" if row["total"] else "never"

    def probe_by_id(self, probe_id: int) -> PendingProbe | None:
        """The stored probe, whether or not it is still pending.

        No TTL or asked filter: the record path targets a probe `serve` already
        named, and a double record is refused by UNIQUE(probe_id) at write time,
        not by this read. Legacy free-text rows (`options IS NULL`) stay
        invisible here for the same reason `next_probe` never serves them.
        """
        row = self.conn.execute(
            "SELECT p.id, p.question, p.options, p.correct_idx, p.explanation,"
            " p.created_at, s.topic, s.hypothesis"
            " FROM probes p"
            " JOIN seeds s ON s.id = p.seed_id"
            " WHERE p.id = ? AND p.options IS NOT NULL",
            (probe_id,),
        ).fetchone()

        return None if row is None else _pending_from_row(row)

    def record_ask(self, interrogation: Interrogation) -> int:
        """Persist one interrogation across both tables, or neither.

        A single transaction because a committed `asks` row with no `answers`
        rows would consume the probe — UNIQUE(probe_id) means it can never be
        asked again — while losing what the developer actually said.
        """
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                # `confidence` is not in the column list. The confidence round
                # was cut, so no interrogation has carried one since — it was a
                # field that could only ever be None, threaded from a dataclass
                # through a bind parameter to write a NULL the column already
                # defaults to. The column itself stays: rows from before the cut
                # hold real numbers, and dropping it would throw those away.
                "INSERT INTO asks"
                " (probe_id, asked_at, outcome, objection, turns, cost_usd,"
                "  completed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    interrogation.probe_id,
                    now,
                    interrogation.outcome,
                    interrogation.objection,
                    len(interrogation.turns),
                    interrogation.cost_usd,
                    now,
                ),
            )
            ask_id = _inserted_id(cursor)

            for turn in interrogation.turns:
                self.conn.execute(
                    "INSERT INTO answers (ask_id, turn, question, answer, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (ask_id, turn.turn, turn.question, turn.answer, now),
                )

        return ask_id

    def unprobed_seeds(self) -> list[UnprobedSeed]:
        """Seeds that never got a question, newest first.

        Two paths leave one behind: stage 4 discarding what stage 3 wrote, and
        stage 2 succeeding into a stage 3 that gave up. Both keep the seed on
        purpose — the moment was real and the hypothesis is as good as it would
        have been had the rest worked — but until `reprobe` nothing could pick
        one back up, which made "the seed is still stored" a claim with no
        redemption behind it.

        Bounded by `PROBE_TTL_DAYS` because stage 3 needs the dialogue, not just
        the seed: re-asking a seed whose probe would be born expired spends a
        model call on a question `next_probe` will never serve. That is the same
        window `next_probe` enforces and it is not separately tunable — it was a
        parameter, and in every call it has ever had it took its default.

        `transcript_path` comes back unchecked. Transcripts rotate, and whether
        this one still exists is a filesystem question the caller has to ask at
        the moment it reads the file anyway.
        """
        cutoff = self._ttl_cutoff()
        rows = self.conn.execute(
            "SELECT s.id, s.session_id, s.turn, s.signal, s.topic, s.quotes, s.refs,"
            "  s.decision, s.hypothesis, s.cost_usd, s.duration_ms, ss.transcript_path"
            " FROM seeds s"
            " JOIN sessions ss ON ss.session_id = s.session_id"
            " LEFT JOIN probes p ON p.seed_id = s.id"
            " WHERE p.id IS NULL AND s.created_at >= :cutoff"
            " ORDER BY s.created_at DESC, s.id DESC",
            {"cutoff": cutoff},
        ).fetchall()
        return [
            UnprobedSeed(
                seed_id=row["id"],
                transcript_path=Path(row["transcript_path"]),
                seed=Seed(
                    session_id=row["session_id"],
                    turn=row["turn"],
                    signal=row["signal"],
                    topic=row["topic"],
                    quotes=tuple(_json_list(row["quotes"])),
                    refs=tuple(_json_list(row["refs"])),
                    decision=row["decision"],
                    hypothesis=row["hypothesis"],
                    cost_usd=row["cost_usd"],
                    duration_ms=row["duration_ms"],
                ),
            )
            for row in rows
        ]

    def stats(self, *, recent: int = 10) -> Stats:
        """The developer's own record: what was asked, and how it went.

        Everything grask measured was measured for whoever is tuning the
        pipeline — keep rates, spend, discard rates, all of it read out of
        sqlite by hand or by a `python -m` batch tool. The person the questions
        are *for* had no way to see the twenty-five they had answered, which
        made the one durable artifact of using grask the one thing it would not
        show them.

        `pending` uses the same TTL and NULL-options filters as `next_probe`, so
        the number here is what `/grask` would actually serve rather than a
        count of rows. Two views of the queue that disagree are worse than one.
        """
        cutoff = self._ttl_cutoff()
        counts = self.conn.execute(
            "SELECT"
            "  (SELECT COUNT(*) FROM sessions WHERE verdict != :capturing) AS sessions,"
            "  (SELECT COUNT(*) FROM probes) AS raised,"
            "  (SELECT COUNT(*) FROM asks) AS answered,"
            "  (SELECT COUNT(*) FROM asks WHERE outcome = 'passed') AS passed,"
            "  (SELECT COUNT(*) FROM asks WHERE outcome = 'failed') AS failed,"
            "  (SELECT COUNT(*) FROM asks WHERE outcome = 'skipped') AS skipped,"
            "  (SELECT COUNT(*) FROM probes p LEFT JOIN asks a ON a.probe_id = p.id"
            "     WHERE a.id IS NULL AND p.created_at >= :cutoff"
            "     AND p.options IS NOT NULL) AS pending",
            {"capturing": CAPTURING, "cutoff": cutoff},
        ).fetchone()

        rows = self.conn.execute(
            "SELECT a.asked_at, a.outcome, s.topic, p.question"
            " FROM asks a"
            " JOIN probes p ON p.id = a.probe_id"
            " JOIN seeds s ON s.id = p.seed_id"
            " ORDER BY a.asked_at DESC, a.id DESC"
            " LIMIT :limit",
            {"limit": recent},
        ).fetchall()

        return Stats(
            sessions=counts["sessions"],
            raised=counts["raised"],
            answered=counts["answered"],
            passed=counts["passed"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            pending=counts["pending"],
            recent=tuple(
                AnsweredProbe(
                    asked_at=row["asked_at"],
                    outcome=row["outcome"],
                    topic=row["topic"],
                    question=row["question"],
                )
                for row in rows
            ),
        )

    def add_probe(self, seed_id: int, probe: Probe) -> int:
        """Store the question, its shuffled options, and the answer key.

        Not the whole rubric: `topic` and `hypothesis` are already on the seed,
        and the rubric is reassembled from both at serve time.

        `criteria` is written as an empty list: the column is NOT NULL in every
        database that predates multiple choice, and rewriting the table to relax
        it risks the live data for no query we run. Legacy rows keep theirs.
        """
        cursor = self.conn.execute(
            "INSERT INTO probes"
            " (seed_id, question, criteria, options, correct_idx, explanation,"
            "  cost_usd, duration_ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seed_id,
                probe.question,
                json.dumps([]),
                json.dumps(list(probe.options)),
                probe.correct_idx,
                probe.explanation,
                probe.cost_usd,
                probe.duration_ms,
                _now(),
            ),
        )
        self.conn.commit()
        return _inserted_id(cursor)
