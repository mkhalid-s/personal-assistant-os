from __future__ import annotations

import json
import sqlite3

from .db import append_event
from .privacy import apply_privacy_filters


def _json_list(values: list[str] | tuple[str, ...] | None) -> str:
    cleaned = [str(v).strip() for v in (values or []) if str(v).strip()]
    return json.dumps(cleaned, ensure_ascii=True)


def create_intent(
    conn: sqlite3.Connection,
    *,
    objective: str,
    context: str = "",
    constraints: list[str] | None = None,
    success_criteria: str = "",
    priority: int = 2,
) -> int:
    objective = apply_privacy_filters(conn, objective or "").strip()
    context = apply_privacy_filters(conn, context or "").strip()
    success_criteria = apply_privacy_filters(conn, success_criteria or "").strip()
    safe_constraints = [apply_privacy_filters(conn, c) for c in (constraints or [])]
    if not objective:
        raise ValueError("objective is required")
    conn.execute(
        """
        INSERT INTO intents (objective, context, constraints_json, success_criteria, priority, status)
        VALUES (?, ?, ?, ?, ?, 'open')
        """,
        (objective, context, _json_list(safe_constraints), success_criteria, int(priority)),
    )
    intent_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        conn,
        "intent_created",
        "intent",
        intent_id,
        json.dumps({"objective": objective[:200], "priority": int(priority)}, ensure_ascii=True),
    )
    return intent_id


def list_intents(conn: sqlite3.Connection, *, status: str = "open", limit: int = 20) -> list[dict]:
    if status == "all":
        rows = conn.execute(
            """
            SELECT id, objective, status, priority, success_criteria, created_at, updated_at
            FROM intents
            ORDER BY status ASC, priority ASC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, objective, status, priority, success_criteria, created_at, updated_at
            FROM intents
            WHERE status = ?
            ORDER BY priority ASC, id DESC
            LIMIT ?
            """,
            (status, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_intent(conn: sqlite3.Connection, intent_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM intents WHERE id = ?", (int(intent_id),)).fetchone()
    if not row:
        return None
    intent = dict(row)
    try:
        intent["constraints"] = json.loads(intent.get("constraints_json") or "[]")
    except (TypeError, ValueError):
        intent["constraints"] = []
    intent["evidence"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, source_type, source_id, summary, content, confidence, created_at
            FROM intent_evidence
            WHERE intent_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(intent_id),),
        ).fetchall()
    ]
    intent["decisions"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, decision, rationale, status, superseded_by, created_at
            FROM intent_decisions
            WHERE intent_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(intent_id),),
        ).fetchall()
    ]
    intent["risks"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, risk, impact, likelihood, mitigation, owner, due_date, status, created_at
            FROM intent_risks
            WHERE intent_id = ?
            ORDER BY status ASC, due_date IS NULL, due_date ASC, id DESC
            """,
            (int(intent_id),),
        ).fetchall()
    ]
    return intent


def add_evidence(
    conn: sqlite3.Connection,
    *,
    intent_id: int,
    content: str,
    source_type: str = "note",
    source_id: str | None = None,
    summary: str = "",
    confidence: float = 0.7,
) -> int:
    if get_intent(conn, intent_id) is None:
        raise ValueError(f"intent #{intent_id} not found")
    source_type = (source_type or "note").strip() or "note"
    summary = apply_privacy_filters(conn, summary or "").strip()
    content = apply_privacy_filters(conn, content or "").strip()
    if not content:
        raise ValueError("evidence text is required")
    conn.execute(
        """
        INSERT INTO intent_evidence (intent_id, source_type, source_id, summary, content, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (int(intent_id), source_type, source_id, summary, content, float(confidence)),
    )
    evidence_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        conn,
        "intent_evidence_added",
        "intent_evidence",
        evidence_id,
        json.dumps({"intent_id": int(intent_id), "source_type": source_type}, ensure_ascii=True),
    )
    return evidence_id
