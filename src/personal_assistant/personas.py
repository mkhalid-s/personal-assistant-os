"""Durable persona manifests that can narrow retrieval and action scope."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .privacy import apply_privacy_filters

BUILTIN_PERSONAS: tuple[dict[str, Any], ...] = (
    {
        "name": "chief-of-staff",
        "display_name": "Chief of Staff",
        "description": "Prioritizes commitments, risks, decisions, and follow-through.",
        "instructions": "Clarify the outcome, surface tradeoffs, identify owners and deadlines, and propose the smallest useful next actions.",
        "allowed_actions": [
            "create_inbox_item",
            "remember",
            "update_people",
            "draft_message",
            "draft_external_update",
            "draft_summary",
        ],
        "retrieval_scopes": ["intents", "work_items", "external_items", "people", "local_memory"],
    },
    {
        "name": "researcher",
        "display_name": "Researcher",
        "description": "Builds evidence-backed answers and calls out missing context.",
        "instructions": "Prefer cited evidence, distinguish facts from inference, expose uncertainty, and do not propose external mutations.",
        "allowed_actions": ["create_inbox_item", "remember", "draft_summary"],
        "retrieval_scopes": ["intents", "work_items", "external_items", "local_memory"],
    },
    {
        "name": "coach",
        "display_name": "Coach",
        "description": "Supports reflection, decisions, habits, and accountable follow-up.",
        "instructions": "Ask what success means, reflect patterns without overclaiming, and turn advice into one concrete, user-owned next step.",
        "allowed_actions": ["create_inbox_item", "remember", "update_people", "draft_summary"],
        "retrieval_scopes": ["people", "local_memory", "work_items"],
    },
    {
        "name": "reviewer",
        "display_name": "Reviewer",
        "description": "Challenges plans, evidence, safety, and completion claims.",
        "instructions": "Look for missing evidence, unsafe assumptions, unverified outcomes, rollback gaps, and approval bypasses.",
        "allowed_actions": ["create_inbox_item", "draft_message", "draft_summary"],
        "retrieval_scopes": ["intents", "work_items", "external_items", "local_memory"],
    },
    {
        "name": "operator",
        "display_name": "Operator",
        "description": "Coordinates bounded operational follow-through.",
        "instructions": "Prefer reversible steps, make side effects explicit, preserve audit context, and leave external changes pending approval.",
        "allowed_actions": ["create_inbox_item", "draft_message", "draft_external_update", "draft_summary"],
        "retrieval_scopes": ["intents", "work_items", "external_items", "local_memory"],
    },
    {
        "name": "engineer",
        "display_name": "Engineer",
        "description": "Plans and reviews repository-scoped technical work.",
        "instructions": "Define validation first, keep changes bounded, preserve existing behavior, and require review before applying patches or external updates.",
        "allowed_actions": ["create_inbox_item", "apply_patch", "draft_external_update", "draft_summary"],
        "retrieval_scopes": ["intents", "work_items", "external_items", "local_memory"],
    },
)


def _decode_list(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _row_to_persona(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "display_name": str(row["display_name"]),
        "description": str(row["description"] or ""),
        "instructions": str(row["instructions"]),
        "allowed_actions": _decode_list(row["allowed_actions_json"]),
        "retrieval_scopes": _decode_list(row["retrieval_scopes_json"]),
        "default_backend": str(row["default_backend"] or ""),
        "is_builtin": bool(row["is_builtin"]),
        "status": str(row["status"]),
    }


# PAOS-041: the builtin seed is immutable within a process, but list/get/
# create surfaces call ensure_builtin_personas on every invocation — each
# call runs an upsert per persona plus a commit. This once-per-process gate
# skips the writes once builtins are known-present. The existence re-check
# keeps behavior identical when a process touches more than one database
# (a second, still-empty personas table must be seeded too), while the
# steady-state path is a single indexed SELECT instead of N upserts.
_BUILTIN_PERSONAS_SEEDED = False


def ensure_builtin_personas(conn: sqlite3.Connection) -> None:
    global _BUILTIN_PERSONAS_SEEDED
    if (
        _BUILTIN_PERSONAS_SEEDED
        and conn.execute("SELECT 1 FROM personas WHERE is_builtin=1 LIMIT 1").fetchone() is not None
    ):
        return
    for persona in BUILTIN_PERSONAS:
        conn.execute(
            """
            INSERT INTO personas (
                name, display_name, description, instructions,
                allowed_actions_json, retrieval_scopes_json, is_builtin, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 'active')
            ON CONFLICT(name) DO UPDATE SET
                display_name=CASE WHEN personas.is_builtin=1 THEN excluded.display_name ELSE personas.display_name END,
                description=CASE WHEN personas.is_builtin=1 THEN excluded.description ELSE personas.description END,
                instructions=CASE WHEN personas.is_builtin=1 THEN excluded.instructions ELSE personas.instructions END,
                allowed_actions_json=CASE WHEN personas.is_builtin=1 THEN excluded.allowed_actions_json ELSE personas.allowed_actions_json END,
                retrieval_scopes_json=CASE WHEN personas.is_builtin=1 THEN excluded.retrieval_scopes_json ELSE personas.retrieval_scopes_json END,
                updated_at=CASE WHEN personas.is_builtin=1 THEN CURRENT_TIMESTAMP ELSE personas.updated_at END
            """,
            (
                persona["name"],
                persona["display_name"],
                persona["description"],
                persona["instructions"],
                json.dumps(persona["allowed_actions"], ensure_ascii=True),
                json.dumps(persona["retrieval_scopes"], ensure_ascii=True),
            ),
        )
    conn.commit()
    _BUILTIN_PERSONAS_SEEDED = True


def list_personas(conn: sqlite3.Connection, *, active_only: bool = True) -> list[dict[str, Any]]:
    ensure_builtin_personas(conn)
    where = "WHERE status='active'" if active_only else ""
    rows = conn.execute(f"SELECT * FROM personas {where} ORDER BY is_builtin DESC, name ASC").fetchall()
    return [_row_to_persona(row) for row in rows]


def get_persona(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    ensure_builtin_personas(conn)
    row = conn.execute(
        "SELECT * FROM personas WHERE name=? AND status='active'", ((name or "").strip().lower(),)
    ).fetchone()
    return _row_to_persona(row) if row else None


def create_persona(
    conn: sqlite3.Connection,
    *,
    name: str,
    display_name: str,
    description: str,
    instructions: str,
    allowed_actions: list[str],
    retrieval_scopes: list[str],
    default_backend: str = "",
) -> dict[str, Any]:
    ensure_builtin_personas(conn)
    slug = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", slug):
        raise ValueError("persona name must be a 2-40 character lowercase slug")
    if not instructions.strip():
        raise ValueError("persona instructions are required")
    safe_instructions = apply_privacy_filters(conn, instructions.strip())[:4000]
    safe_description = apply_privacy_filters(conn, description.strip())[:500]
    safe_display_name = apply_privacy_filters(conn, (display_name or slug).strip())[:120]
    actions = list(dict.fromkeys(item.strip() for item in allowed_actions if item.strip()))
    scopes = list(dict.fromkeys(item.strip() for item in retrieval_scopes if item.strip()))
    try:
        conn.execute(
            """
            INSERT INTO personas (
                name, display_name, description, instructions,
                allowed_actions_json, retrieval_scopes_json, default_backend
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                safe_display_name,
                safe_description,
                safe_instructions,
                json.dumps(actions, ensure_ascii=True),
                json.dumps(scopes, ensure_ascii=True),
                default_backend.strip()[:80],
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"persona already exists: {slug}") from exc
    conn.commit()
    persona = get_persona(conn, slug)
    assert persona is not None
    return persona


def filter_actions(
    persona: dict[str, Any], actions: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[str]]:
    allowed = {str(item) for item in persona.get("allowed_actions", [])}
    accepted: list[dict[str, object]] = []
    rejected: list[str] = []
    for action in actions:
        action_type = str(action.get("action_type") or "")
        if action_type in allowed:
            accepted.append(action)
        else:
            rejected.append(action_type or "unknown")
    return accepted, rejected


def retrieval_source_types(persona: dict[str, Any]) -> set[str]:
    """Translate a persona's logical retrieval scopes to indexed source types."""
    scopes = {str(item) for item in persona.get("retrieval_scopes", [])}
    mapping = {
        "work_items": {"work_item"},
        "external_items": {"external_item"},
        # Durable memories currently enter the shared index through conversations
        # and ingested media. Agent observations are retrieved separately by planner.
        "local_memory": {"conversation", "media_asset"},
        "intents": {"intent"},
        "people": {"person", "evidence", "one_on_one"},
    }
    return set().union(*(mapping.get(scope, set()) for scope in scopes))
