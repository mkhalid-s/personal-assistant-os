"""Shared agent primitives used by every backend.

These functions are deliberately dependency-light (db + json only) so that
``providers`` and ``assistant`` can import them without creating an import
cycle with ``cli``. Every proposed action -- regardless of which backend produced
it -- flows through :func:`enqueue_proposal` into the
existing ``agent_actions`` approval queue, so propose-and-approve is enforced in
one place.
"""

from __future__ import annotations

import json

from . import autonomy
from .db import append_event

# Single source of truth (derived from autonomy._ACTION_TIER) for which action
# types may run without approval. Importing keeps this from drifting from the
# tier table / classifier (review finding #11).
AUTO_SAFE_ACTION_TYPES = autonomy.AUTO_ACTION_TYPES


def ensure_turn_task(conn, objective: str, context: str = "") -> int:
    """Create an ``agent_tasks`` row to hold the proposals from one turn/run."""
    conn.execute(
        """
        INSERT INTO agent_tasks (objective, context, constraints_json, priority, status)
        VALUES (?, ?, '{}', 2, 'open')
        """,
        ((objective.strip()[:2000] or "assistant turn"), context.strip()[:2000]),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def enqueue_proposal(
    conn,
    *,
    task_id: int,
    action_type: str,
    title: str,
    payload: dict,
    requires_approval: int = 1,
) -> int:
    """Insert one proposed action and return its id.

    External mutations must keep ``requires_approval=1``; nothing executes here --
    execution only happens later via ``cmd_act`` / ``cmd_approve``.

    Redacts title and payload leaf values so model/backend-proposed PII (emails,
    phones, secrets echoed from user_text or context) never lands in agent_actions
    in cleartext, consistent with every other persistence chokepoint (review R4-2).
    """
    from .privacy import apply_privacy_filters, redact_obj

    title = apply_privacy_filters(conn, title or "")
    payload = redact_obj(conn, payload)
    if action_type not in AUTO_SAFE_ACTION_TYPES:
        requires_approval = 1
    conn.execute(
        """
        INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, requires_approval, status)
        VALUES (?, ?, ?, ?, ?, 'proposed')
        """,
        (task_id, action_type, title[:500], json.dumps(payload, ensure_ascii=True), int(requires_approval)),
    )
    action_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        conn,
        "agent_propose",
        "agent_action",
        action_id,
        json.dumps({"action_type": action_type, "task_id": task_id}, ensure_ascii=True),
    )
    return action_id


def capture_item(
    conn,
    *,
    text: str,
    kind: str = "task",
    owner: str | None = None,
    due_date: str | None = None,
    source: str = "assistant",
) -> tuple[int | None, bool]:
    """Safe local capture into the inbox, deduped by exact text.

    Returns ``(inbox_id, created)``; ``created`` is False when an identical row
    already existed. Mirrors ``cli.insert_inbox_item_dedup`` but stays import-free.

    Redacts the captured text — this is the chokepoint for assistant/em-driven inbox
    writes (note->inbox fallback, 1:1/meeting action items), so PII never reaches
    inbox_items (and, via triage, the FTS index) in cleartext (findings #2/#7).
    """
    from .privacy import apply_privacy_filters

    text = apply_privacy_filters(conn, text or "")
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO inbox_items (text, kind, owner, due_date, confidence, source, status)
        VALUES (?, ?, ?, ?, ?, ?, 'new')
        """,
        (text.strip(), kind, owner, due_date, 0.8, source),
    )
    if cur.rowcount == 0:
        return None, False
    inbox_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    return inbox_id, True


def remember(conn, text: str, *, source_type: str = "memory", source_id: int = 0) -> int | None:
    """Persist a fact to long-term memory (text_chunks). The FTS5 triggers index
    it automatically, so it is immediately recall-able across sessions.

    Redacts here — this is the chokepoint for the conversation/memory text_chunks path
    (the model-supplied `remember` tool and any future caller of this fn), so applying the
    privacy filter once closes them in one place (review #6). The OTHER text_chunks writer is
    inbox.index_chunk (work_item titles, media), which redacts independently; there is no
    single chokepoint for the whole table. Callers that pre-redact (e.g. context.log_turn)
    just get an idempotent second pass."""
    from .privacy import apply_privacy_filters

    text = apply_privacy_filters(conn, (text or "").strip())
    if not text:
        return None
    conn.execute(
        "INSERT INTO text_chunks (source_type, source_id, content) VALUES (?, ?, ?)",
        (source_type, source_id, text),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
