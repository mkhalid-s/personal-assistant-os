"""Pipeline mutual-exclusion locks (SQLite-backed).

Extracted from cli.py (refactor #12). Used by the always-on loops (autopilot/pulse)
and run_day/go_live to prevent overlapping cycles.
"""

from __future__ import annotations

import contextlib
import sqlite3

# PAOS-019: a lock older than this is considered abandoned — its holder crashed
# without releasing and the name is free to reclaim. Raised from 1h to 4h because
# long autopilot/factory cycles and interactive run_day/go_live sessions
# legitimately hold a lock for over an hour; a too-aggressive threshold let a
# second instance steal the lock mid-cycle.
LOCK_STALE_AFTER_HOURS = 4


def acquire_lock(conn, name: str, owner: str) -> bool:
    # BEGIN IMMEDIATE makes the stale-reclaim + claim atomic against other writers;
    # under contention we return False rather than crashing (review finding #19).
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"DELETE FROM pipeline_locks WHERE name = ? AND acquired_at < datetime('now', '-{LOCK_STALE_AFTER_HOURS} hours')",
            (name,),
        )
        conn.execute("INSERT OR IGNORE INTO pipeline_locks (name, owner) VALUES (?, ?)", (name, owner))
        row = conn.execute("SELECT owner FROM pipeline_locks WHERE name = ?", (name,)).fetchone()
        conn.commit()
        return bool(row and row["owner"] == owner)
    except sqlite3.OperationalError as exc:
        # Only treat genuine contention (locked/busy) as "couldn't acquire". A
        # txn-state error ("cannot start a transaction within a transaction") means a
        # caller invoked us with an open transaction — re-raise it instead of masking
        # it as contention AND silently rolling back the caller's buffered work (B4).
        msg = str(exc).lower()
        if "lock" in msg or "busy" in msg:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            return False
        raise


def renew_lock(conn, name: str, owner: str) -> None:
    """Refresh ``acquired_at`` for a lock we hold (PAOS-019).

    Long-running loops that hold a pipeline lock across a whole cycle call this
    once per cycle so the lock never ages past ``LOCK_STALE_AFTER_HOURS`` while
    legitimate work is still in flight. Only refreshes when ``owner`` still owns
    the lock — a stolen/released lock is never resurrected."""
    conn.execute(
        "UPDATE pipeline_locks SET acquired_at = CURRENT_TIMESTAMP WHERE name = ? AND owner = ?",
        (name, owner),
    )
    conn.commit()


def release_lock(conn, name: str, owner: str) -> None:
    conn.execute(
        "DELETE FROM pipeline_locks WHERE name = ? AND owner = ?",
        (name, owner),
    )
    conn.commit()  # finding #8: commit so the DELETE doesn't leave an open write txn
