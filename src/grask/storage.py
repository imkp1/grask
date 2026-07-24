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
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    "sessions": (("duration_ms", "INTEGER"),),
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
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # Off by default in sqlite, and the one thing keeping an orphaned seed
        # from outliving the session that explains it.
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

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

    def has_session(self, session_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None

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
    ) -> None:
        """Record one triaged session. A second call for the same id does nothing.

        `cost_usd` and `duration_ms` are triage's alone. Seed and probe carry
        their own, because a single column that means different things per
        verdict is a column nobody can sum.

        Both are None on the stage-0 path, which records a silent session
        without ever calling a model.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions"
            " (session_id, transcript_path, cwd, git_branch, verdict, signal, topic,"
            "  cost_usd, duration_ms, triaged_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                transcript_path,
                cwd,
                git_branch,
                verdict,
                signal,
                topic,
                cost_usd,
                duration_ms,
                _now(),
            ),
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
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PROBE_TTL_DAYS)).isoformat()
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
                "INSERT INTO asks"
                " (probe_id, asked_at, confidence, outcome, objection, turns, cost_usd,"
                "  completed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    interrogation.probe_id,
                    now,
                    interrogation.confidence,
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
