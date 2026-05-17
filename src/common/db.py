# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""SQLite connection helpers and schema initialization."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a sqlite connection with our standard pragmas applied.

    Caller is responsible for closing.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Note: deliberately not using detect_types=PARSE_DECLTYPES.
    # We store timestamps as ISO 8601 strings exactly as GitHub returns
    # them (e.g. "2026-05-15T08:50:57Z"). sqlite3's built-in TIMESTAMP
    # converter expects "YYYY-MM-DD HH:MM:SS" (space separator) and
    # crashes on the ISO 8601 form. Keeping values as text throughout
    # is simpler and lets the analysis layer parse them itself.
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # autocommit; explicit BEGIN/COMMIT blocks below
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # synchronous = FULL waits for fsync on every commit. Slower than
    # NORMAL but more robust against partial writes during an unclean
    # process exit. For our extraction throughput the cost is
    # negligible compared to the cost of a corrupt database.
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def checkpoint(conn: sqlite3.Connection) -> None:
    """Force a WAL checkpoint, flushing committed pages into the main
    file and resetting the WAL.

    Call at phase boundaries during long-running extractions so the WAL
    does not grow unbounded; a smaller WAL means less unflushed state
    at risk during an unclean exit.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        # Checkpoint can fail if other readers are active; not fatal.
        pass


class IntegrityError(Exception):
    """Raised when a SQLite integrity check returns a non-ok result."""


def integrity_check_full(conn: sqlite3.Connection) -> None:
    """Run ``PRAGMA integrity_check``. Raise IntegrityError on failure.

    Full check; reads every page and verifies index<->row consistency.
    On a healthy database returns a single row with ``"ok"``. On
    corruption returns one or more rows describing the damage.
    """
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if not rows or len(rows) != 1 or rows[0][0] != "ok":
        msgs = [r[0] for r in rows[:20]]
        more = "" if len(rows) <= 20 else f" (and {len(rows) - 20} more)"
        raise IntegrityError("integrity_check failed: " + "; ".join(msgs) + more)


class HourlyChecker:
    """Tracks elapsed wall-clock time and runs a full integrity check
    whenever an hour has passed since the last check.

    Per-item phase loops call ``maybe_check(conn)`` once per item; the
    check runs at most once per hour and its cost (a single full
    integrity_check) is amortized across the per-item iteration.
    """

    def __init__(self, interval_seconds: float = 3600.0) -> None:
        import time as _t
        self._time = _t
        self.interval = interval_seconds
        self._last = _t.monotonic()

    def maybe_check(self, conn: sqlite3.Connection) -> bool:
        """Return True if a check was performed."""
        now = self._time.monotonic()
        if now - self._last < self.interval:
            return False
        # Checkpoint first so the check sees the current state, not just
        # what's already in the main file.
        checkpoint(conn)
        integrity_check_full(conn)
        self._last = now
        return True


def vacuum_into(conn: sqlite3.Connection, dest_path: Path) -> None:
    """Write a clean copy of the database to ``dest_path``.

    Uses ``VACUUM INTO`` so the result is a defragmented, transaction-safe
    snapshot. Removes any pre-existing file at the destination first
    because VACUUM INTO refuses to overwrite.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()
    # The destination path is interpolated into SQL because PRAGMA/VACUUM
    # don't accept bound parameters for filenames. We control the value
    # (it comes from our config), so this is safe; we still escape any
    # single quotes defensively.
    escaped = str(dest_path).replace("'", "''")
    conn.execute(f"VACUUM INTO '{escaped}'")


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply the DDL from schema.sql. Idempotent (CREATE IF NOT EXISTS)."""
    sql = SCHEMA_PATH.read_text()
    conn.executescript(sql)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block as a single transaction. Rollback on exception."""
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM extraction_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO extraction_state (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value),
    )
