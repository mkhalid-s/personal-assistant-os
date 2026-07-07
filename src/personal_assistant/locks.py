"""Pipeline mutual-exclusion locks (SQLite-backed).

Extracted from cli.py (refactor #12). Used by the always-on loops (autopilot/pulse)
and run_day/go_live to prevent overlapping cycles.
"""

from __future__ import annotations

import contextlib
import sqlite3


def acquire_lock(conn, name: str, owner: str) -> bool:
    # BEGIN IMMEDIATE makes the stale-reclaim + claim atomic against other writers;
    # under contention we return False rather than crashing (review finding #19).
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM pipeline_locks WHERE name = ? AND acquired_at < datetime('now', '-1 hour')",
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


def release_lock(conn, name: str, owner: str) -> None:
    conn.execute(
        "DELETE FROM pipeline_locks WHERE name = ? AND owner = ?",
        (name, owner),
    )
    conn.commit()  # finding #8: commit so the DELETE doesn't leave an open write txn
