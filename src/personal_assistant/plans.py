from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import intents
from .db import append_event
from .privacy import apply_privacy_filters


def _json_list(values: list[str] | tuple[str, ...] | None) -> str:
    cleaned = [str(v).strip() for v in (values or []) if str(v).strip()]
    return json.dumps(cleaned, ensure_ascii=True)


def create_plan(
    conn: sqlite3.Connection,
    *,
    intent_id: int,
    title: str = "",
    assumptions: list[str] | None = None,
) -> int:
    intent = intents.get_intent(conn, int(intent_id))
    if intent is None:
        raise ValueError(f"intent #{intent_id} not found")
    title = apply_privacy_filters(conn, title or intent["objective"]).strip()
    safe_assumptions = [apply_privacy_filters(conn, item) for item in (assumptions or [])]
    summary = f"Draft plan for intent #{intent_id}: {intent['objective']}"
    conn.execute(
        """
        INSERT INTO plans (intent_id, title, summary, assumptions_json, status)
        VALUES (?, ?, ?, ?, 'draft')
        """,
        (int(intent_id), title, summary, _json_list(safe_assumptions)),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    default_steps = [
        ("Gather and attach the strongest evidence for the intent.", "Evidence is cited or explicitly missing."),
        ("Identify risks, dependencies, and approval requirements.", "Risks and approval gates are documented."),
        ("Execute only approved low-risk actions and capture receipts.", "Receipts or follow-up work items exist."),
    ]
    for index, (description, validation) in enumerate(default_steps, start=1):
        conn.execute(
            """
            INSERT INTO plan_steps (plan_id, step_index, description, validation)
            VALUES (?, ?, ?, ?)
            """,
            (plan_id, index, description, validation),
        )
    conn.execute(
        """
        INSERT INTO plan_risks (plan_id, risk, mitigation, severity)
        VALUES (?, ?, ?, 'medium')
        """,
        (
            plan_id,
            "Plan may act on incomplete or stale context.",
            "Require cited evidence and human approval before external mutation.",
        ),
    )
    conn.execute(
        """
        INSERT INTO plan_validations (plan_id, check_name, command, expected)
        VALUES (?, 'review_packet_complete', ?, ?)
        """,
        (plan_id, f"myos review-packet --plan {plan_id}", "packet includes intent, evidence, risks, and validations"),
    )
    append_event(
        conn,
        "plan_created",
        "plan",
        plan_id,
        json.dumps({"intent_id": int(intent_id), "title": title[:200]}, ensure_ascii=True),
    )
    return plan_id


def list_plans(
    conn: sqlite3.Connection,
    *,
    intent_id: int | None = None,
    status: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent plans with optional intent/status filters.

    Kept as a narrow projection (id, intent_id, title, summary, status,
    timestamps) so callers reason about the plan queue without loading the
    per-plan steps/risks/validations — those live on ``get_plan``."""
    clauses = []
    params: list[Any] = []
    if intent_id is not None:
        clauses.append("intent_id = ?")
        params.append(int(intent_id))
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT id, intent_id, title, summary, status, created_at, updated_at
        FROM plans
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def get_plan(conn: sqlite3.Connection, plan_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (int(plan_id),)).fetchone()
    if not row:
        return None
    plan = dict(row)
    try:
        plan["assumptions"] = json.loads(plan.get("assumptions_json") or "[]")
    except (TypeError, ValueError):
        plan["assumptions"] = []
    plan["steps"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, step_index, description, owner, status, validation
            FROM plan_steps
            WHERE plan_id = ?
            ORDER BY step_index ASC
            """,
            (int(plan_id),),
        ).fetchall()
    ]
    plan["risks"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, risk, mitigation, severity, status
            FROM plan_risks
            WHERE plan_id = ?
            ORDER BY id ASC
            """,
            (int(plan_id),),
        ).fetchall()
    ]
    plan["validations"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, check_name, command, expected, status
            FROM plan_validations
            WHERE plan_id = ?
            ORDER BY id ASC
            """,
            (int(plan_id),),
        ).fetchall()
    ]
    return plan


def attach_retrieval_run_evidence(conn: sqlite3.Connection, *, intent_id: int, retrieval_run_id: int) -> int:
    run = conn.execute(
        "SELECT id, query, mode, selected_count FROM retrieval_runs WHERE id = ?",
        (int(retrieval_run_id),),
    ).fetchone()
    if not run:
        raise ValueError(f"retrieval run #{retrieval_run_id} not found")
    sources = conn.execute(
        """
        SELECT citation, reason
        FROM retrieval_run_sources
        WHERE retrieval_run_id = ?
        ORDER BY rank ASC
        LIMIT 5
        """,
        (int(retrieval_run_id),),
    ).fetchall()
    citations = ", ".join(row["citation"] for row in sources) or "none"
    content = (
        f"Retrieval run #{run['id']} [{run['mode']}] query={run['query']} "
        f"selected={run['selected_count']} citations={citations}"
    )
    return intents.add_evidence(
        conn,
        intent_id=int(intent_id),
        content=content,
        source_type="retrieval_run",
        source_id=str(retrieval_run_id),
        summary=f"Retrieval trace for: {run['query']}",
        confidence=0.8,
    )


def create_review_packet(
    conn: sqlite3.Connection,
    *,
    plan_id: int,
    retrieval_run_id: int | None = None,
    executor_artifacts: list[dict[str, Any]] | None = None,
) -> int:
    plan = get_plan(conn, int(plan_id))
    if plan is None:
        raise ValueError(f"plan #{plan_id} not found")
    intent = intents.get_intent(conn, int(plan["intent_id"]))
    if intent is None:
        raise ValueError(f"intent #{plan['intent_id']} not found")
    retrieval_sources: list[dict[str, Any]] = []
    if retrieval_run_id is not None:
        retrieval_sources = [
            dict(row)
            for row in conn.execute(
                """
                SELECT rank, citation, score, reason, graph_path_json, content_preview
                FROM retrieval_run_sources
                WHERE retrieval_run_id = ?
                ORDER BY rank ASC
                """,
                (int(retrieval_run_id),),
            ).fetchall()
        ]
    packet = {
        "intent": {
            "id": intent["id"],
            "objective": intent["objective"],
            "success_criteria": intent.get("success_criteria") or "",
        },
        "plan": {
            "id": plan["id"],
            "title": plan["title"],
            "summary": plan.get("summary") or "",
            "assumptions": plan["assumptions"],
            "steps": plan["steps"],
            "risks": plan["risks"],
            "validations": plan["validations"],
        },
        "evidence": intent["evidence"],
        "retrieval_sources": retrieval_sources,
        "executor_artifacts": executor_artifacts or [],
        "approval_required": True,
        "rollback_note": "Do not mutate external systems without explicit approval and an execution receipt.",
    }
    summary = f"Review packet for plan #{plan_id}: {plan['title']}"
    conn.execute(
        """
        INSERT INTO review_packets (plan_id, intent_id, retrieval_run_id, summary, packet_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(plan_id),
            int(intent["id"]),
            int(retrieval_run_id) if retrieval_run_id is not None else None,
            summary,
            json.dumps(packet, ensure_ascii=True),
        ),
    )
    packet_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        conn,
        "review_packet_created",
        "review_packet",
        packet_id,
        json.dumps({"plan_id": int(plan_id), "intent_id": int(intent["id"])}, ensure_ascii=True),
    )
    return packet_id


def attach_executor_artifact(conn: sqlite3.Connection, *, packet_id: int, artifact: dict[str, Any]) -> None:
    row = conn.execute("SELECT summary, packet_json FROM review_packets WHERE id = ?", (int(packet_id),)).fetchone()
    if row is None:
        raise ValueError(f"review packet #{packet_id} not found")
    try:
        packet = json.loads(row["packet_json"] or "{}")
    except (TypeError, ValueError):
        packet = {}
    artifacts = packet.get("executor_artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    artifacts.append(artifact)
    packet["executor_artifacts"] = artifacts
    conn.execute(
        """
        UPDATE review_packets
        SET packet_json = ?
        WHERE id = ?
        """,
        (
            json.dumps(packet, ensure_ascii=True),
            int(packet_id),
        ),
    )
    append_event(
        conn,
        "review_packet_executor_artifact",
        "review_packet",
        int(packet_id),
        json.dumps({"artifact_type": str(artifact.get("type") or "executor")}, ensure_ascii=True),
    )


def get_review_packet(conn: sqlite3.Connection, packet_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM review_packets WHERE id = ?", (int(packet_id),)).fetchone()
    if not row:
        return None
    packet = dict(row)
    try:
        packet["packet"] = json.loads(packet.get("packet_json") or "{}")
    except (TypeError, ValueError):
        packet["packet"] = {}
    return packet
