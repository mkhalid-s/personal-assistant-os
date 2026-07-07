from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

TRACE_ENV = "MYOS_TRACE_CORRELATION_ID"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_ROWS = 5000
SUMMARY_LIMIT = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def new_correlation_id() -> str:
    return f"trace_{uuid.uuid4().hex[:24]}"


def current_correlation_id() -> str:
    return os.getenv(TRACE_ENV, "").strip()


def start_trace(
    conn: sqlite3.Connection,
    *,
    command: str,
    command_path: str,
    surface: str = "cli",
    parent_correlation_id: str = "",
    argv_hash: str = "",
) -> str:
    correlation_id = new_correlation_id()
    conn.execute(
        """
        INSERT INTO execution_traces (
            correlation_id, parent_correlation_id, surface, command, command_path,
            status, argv_hash, started_at
        )
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (
            correlation_id,
            parent_correlation_id,
            surface,
            command,
            command_path,
            argv_hash,
            _utc_now(),
        ),
    )
    conn.commit()
    return correlation_id


def finish_trace(
    conn: sqlite3.Connection,
    correlation_id: str,
    *,
    status: str,
    duration_ms: int,
    summary: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    safe_summary = (summary or "")[:SUMMARY_LIMIT]
    conn.execute(
        """
        UPDATE execution_traces
           SET status = ?,
               duration_ms = ?,
               summary = ?,
               summary_hash = ?,
               metadata_json = ?,
               finished_at = ?
         WHERE correlation_id = ?
        """,
        (
            status,
            max(0, int(duration_ms)),
            safe_summary,
            _hash_text(safe_summary) if safe_summary else "",
            json.dumps(metadata or {}, ensure_ascii=True)[:2000],
            _utc_now(),
            correlation_id,
        ),
    )
    conn.commit()


def link_trace(
    conn: sqlite3.Connection,
    correlation_id: str,
    *,
    intent: str = "",
    command_tier: str = "",
    safety_level: str = "",
    route_event_id: int | None = None,
    factory_run_id: int | None = None,
    agent_task_id: int | None = None,
    receipt_id: int | None = None,
) -> None:
    if not correlation_id:
        return
    assignments: list[str] = []
    values: list[object] = []
    if intent:
        assignments.append("intent = ?")
        values.append(intent)
    if command_tier:
        assignments.append("command_tier = ?")
        values.append(command_tier)
    if safety_level:
        assignments.append("safety_level = ?")
        values.append(safety_level)
    if route_event_id is not None:
        assignments.append("route_event_id = ?")
        values.append(int(route_event_id))
    if factory_run_id is not None:
        assignments.append("factory_run_id = ?")
        values.append(int(factory_run_id))
    if agent_task_id is not None:
        assignments.append("agent_task_id = ?")
        values.append(int(agent_task_id))
    if receipt_id is not None:
        assignments.append("receipt_id = ?")
        values.append(int(receipt_id))
    if not assignments:
        return
    values.append(correlation_id)
    conn.execute(f"UPDATE execution_traces SET {', '.join(assignments)} WHERE correlation_id = ?", values)


def link_current_trace(conn: sqlite3.Connection, **links: Any) -> None:
    correlation_id = current_correlation_id()
    if correlation_id:
        link_trace(conn, correlation_id, **links)


def list_traces(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    status: str = "",
    command: str = "",
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[object] = []
    if status:
        clauses.append("status = ?")
        values.append(status)
    if command:
        clauses.append("command_path LIKE ?")
        values.append(f"%{command}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, correlation_id, surface, command_path, status, duration_ms,
               intent, command_tier, safety_level, route_event_id, factory_run_id,
               agent_task_id, receipt_id, summary, created_at, started_at, finished_at
          FROM execution_traces
          {where}
         ORDER BY id DESC
         LIMIT ?
        """,
        (*values, max(1, int(limit))),
    ).fetchall()
    return [dict(row) for row in rows]


def _rollup_rows(conn: sqlite3.Connection, where_sql: str, values: tuple[object, ...]) -> int:
    rows = conn.execute(
        f"""
        SELECT substr(started_at, 1, 10) AS bucket_date,
               command_path,
               status,
               COUNT(*) AS trace_count,
               COALESCE(SUM(duration_ms), 0) AS total_duration_ms
          FROM execution_traces
         WHERE {where_sql}
         GROUP BY bucket_date, command_path, status
        """,
        values,
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT INTO execution_trace_rollups (
                bucket_date, command_path, status, trace_count, total_duration_ms, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_date, command_path, status) DO UPDATE SET
                trace_count = trace_count + excluded.trace_count,
                total_duration_ms = total_duration_ms + excluded.total_duration_ms,
                updated_at = excluded.updated_at
            """,
            (
                row["bucket_date"] or "unknown",
                row["command_path"] or "unknown",
                row["status"] or "unknown",
                int(row["trace_count"] or 0),
                int(row["total_duration_ms"] or 0),
                _utc_now(),
            ),
        )
    return sum(int(row["trace_count"] or 0) for row in rows)


def cleanup_traces(
    conn: sqlite3.Connection,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict[str, int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(retention_days)))).replace(microsecond=0)
    cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
    old_count = _rollup_rows(conn, "started_at < ?", (cutoff_text,))
    conn.execute("DELETE FROM execution_traces WHERE started_at < ?", (cutoff_text,))

    remaining = conn.execute("SELECT COUNT(*) AS c FROM execution_traces").fetchone()["c"]
    overflow = max(0, int(remaining) - max(1, int(max_rows)))
    if overflow:
        ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM execution_traces ORDER BY id ASC LIMIT ?", (overflow,)).fetchall()
        ]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            _rollup_rows(conn, f"id IN ({placeholders})", tuple(ids))
            conn.execute(f"DELETE FROM execution_traces WHERE id IN ({placeholders})", tuple(ids))

    conn.commit()
    return {"rolled_up": old_count + overflow, "deleted": old_count + overflow, "remaining": int(remaining) - overflow}


def rollups(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT bucket_date, command_path, status, trace_count, total_duration_ms, updated_at
          FROM execution_trace_rollups
         ORDER BY bucket_date DESC, updated_at DESC
         LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]
