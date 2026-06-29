"""Inbox/index write helpers + deterministic kind/priority/risk inference.

Extracted from cli.py (refactor #12). These are the keyword heuristics used when no
LLM brain is involved (capture, external ingest, the deterministic planner fallback).
"""

from __future__ import annotations

from datetime import date

from .graph import upsert_node


def index_chunk(conn, source_type: str, source_id: int, content: str, provenance_id: int | None = None) -> bool:
    """Write one FTS-indexed text_chunk. Returns True if a row was written, False otherwise.

    Redacts before inserting — this is the second text_chunks write path alongside
    agentcore.remember() (the conversation/memory chokepoint). Media callers already
    pre-redact; the second pass is idempotent. Callers that need to know whether a chunk
    was actually written (e.g. cmd_reindex) should check the return value (review R4-6)."""
    from .privacy import apply_privacy_filters

    content = apply_privacy_filters(conn, content or "")
    if not content.strip():
        return False
    conn.execute(
        "INSERT INTO text_chunks (source_type, source_id, content, provenance_id) VALUES (?, ?, ?, ?)",
        (source_type, source_id, content.strip(), provenance_id),
    )
    return True


def ensure_work_item_node(conn, item_id: int, title: str) -> int:
    return upsert_node(conn, "work_item", item_id, title)


def infer_kind(text: str) -> str:
    t = text.lower()
    if t.startswith("decision:") or " decided " in t:
        return "decision"
    if "follow up" in t or "by " in t or "i will" in t or "i'll" in t:
        return "commitment"
    if "blocker" in t or "blocked" in t or "risk" in t:
        return "risk"
    if "todo" in t or "task" in t or "implement" in t:
        return "task"
    return "note"


def infer_priority(text: str, due_date: str | None) -> int:
    t = text.lower()
    p = 2
    if any(k in t for k in ["urgent", "asap", "today", "critical"]):
        p = 1
    if due_date:
        try:
            d = date.fromisoformat(due_date)
            if (d - date.today()).days <= 1:
                p = 1
        except ValueError:
            pass
    return p


def infer_risk(text: str, due_date: str | None) -> int:
    t = text.lower()
    score = 10
    if any(k in t for k in ["blocked", "dependency", "risk", "escalate"]):
        score += 35
    if any(k in t for k in ["customer", "production", "incident", "security"]):
        score += 30
    if due_date:
        try:
            d = date.fromisoformat(due_date)
            days = (d - date.today()).days
            if days < 0:
                score += 35
            elif days <= 2:
                score += 20
        except ValueError:
            score += 5
    return min(score, 100)


def infer_from_external(item_type: str, title: str, status: str | None) -> tuple[str, int]:
    text = f"{item_type} {title}".lower()
    kind = "task"
    if item_type in ("pull_request",):
        kind = "task"
    elif item_type in ("feature", "issue"):
        kind = "commitment"
    elif "decision" in text:
        kind = "decision"
    if any(k in text for k in ["blocker", "blocked", "dependency", "risk"]):
        kind = "risk"

    priority = 2
    if any(k in text for k in ["urgent", "critical", "p0", "sev1"]):
        priority = 1
    if status and status.lower() in ("in progress", "doing", "open"):
        priority = min(priority, 2)
    return kind, priority


def insert_inbox_item_dedup(
    conn,
    *,
    text: str,
    kind: str,
    owner: str | None,
    due_date: str | None,
    confidence: float,
    source: str,
) -> int | None:
    # Redact here — every inbox writer (external ingest, execution create_inbox_item,
    # EM action items) flows through this dedup helper, making it the single chokepoint
    # for inbox_items.text (review R4-1/R4-3). The redacted form is what is stored AND
    # what the unique index deduplicates on; two inputs differing only in PII may collapse
    # to one row — that is intentional, since they would persist identically anyway (R4-5).
    from .privacy import apply_privacy_filters

    text = apply_privacy_filters(conn, text.strip())
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO inbox_items (text, kind, owner, due_date, confidence, source, status)
        VALUES (?, ?, ?, ?, ?, ?, 'new')
        """,
        (text, kind, owner, due_date, confidence, source),
    )
    if cur.rowcount == 0:
        return None
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
