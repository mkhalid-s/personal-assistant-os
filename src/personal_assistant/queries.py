"""Read-only query helpers exposed to the assistant's read tools.

Written fresh against the canonical tables (rather than refactored out of the
print-only ``cmd_*`` functions) so existing CLI output and its tests stay
untouched. Every function returns plain dicts/lists -- no printing, no mutation --
so any backend can call them safely and auto-run them without approval.
"""

from __future__ import annotations

from .retrieval import hybrid_score, tokenize


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def at_risk(conn, threshold: int = 50, limit: int = 10) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT id, title, kind, status, risk_score, priority, owner, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC, priority ASC
        LIMIT ?
        """,
        (threshold, limit),
    )


def waiting_on(conn, limit: int = 10) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT id, title, owner, status, due_date, risk_score
        FROM work_items
        WHERE status='open' AND owner IS NOT NULL AND owner != ''
        ORDER BY risk_score DESC, due_date IS NULL, due_date ASC
        LIMIT ?
        """,
        (limit,),
    )


def today(conn, meeting_hours: float = 0.0, limit: int = 10) -> dict:
    items = _rows(
        conn,
        """
        SELECT id, title, kind, priority, risk_score, due_date, owner
        FROM work_items
        WHERE status='open'
        ORDER BY priority ASC, risk_score DESC
        LIMIT ?
        """,
        (limit,),
    )
    return {"meeting_hours": meeting_hours, "focus": items}


def risk_radar(conn, limit: int = 10) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT id, title, risk_score, status, owner, due_date
        FROM work_items
        WHERE status='open'
        ORDER BY risk_score DESC
        LIMIT ?
        """,
        (limit,),
    )


def brief(conn, meeting_hours: float = 0.0, top: int = 5, risk_threshold: int = 60) -> dict:
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM inbox_items WHERE status='new') AS inbox_new,
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_work,
          (SELECT COUNT(*) FROM work_items WHERE status='open' AND risk_score >= ?) AS at_risk
        """,
        (risk_threshold,),
    ).fetchone()
    return {
        "meeting_hours": meeting_hours,
        "inbox_new": counts["inbox_new"],
        "open_work": counts["open_work"],
        "at_risk_count": counts["at_risk"],
        "top_outcomes": at_risk(conn, threshold=0, limit=top),
        "at_risk": at_risk(conn, threshold=risk_threshold, limit=top),
    }


def metrics(conn, days: int = 7, risk_threshold: int = 60) -> dict:
    row = conn.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM work_items WHERE created_at >= datetime('now', '-{int(days)} days')) AS new_work,
          (SELECT COUNT(*) FROM work_items WHERE status='done') AS done_work,
          (SELECT COUNT(*) FROM work_items WHERE status='open' AND risk_score >= ?) AS at_risk,
          (SELECT COUNT(*) FROM event_log WHERE created_at >= datetime('now', '-{int(days)} days')) AS events
        """,
        (risk_threshold,),
    ).fetchone()
    return {"window_days": days, **dict(row)}


def why(conn, item_id: int) -> dict:
    item = conn.execute(
        "SELECT id, title, kind, status, risk_score, inbox_id FROM work_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not item:
        return {"error": f"work item #{item_id} not found"}
    provenance = _rows(
        conn,
        """
        SELECT p.extractor, p.confidence, p.snippet, c.content
        FROM text_chunks c
        LEFT JOIN provenance p ON p.id = c.provenance_id
        WHERE c.source_type='work_item' AND c.source_id = ?
        ORDER BY c.created_at ASC
        LIMIT 5
        """,
        (item_id,),
    )
    return {"item": dict(item), "provenance": provenance}


def _fts_match(query: str) -> str:
    toks = tokenize(query)
    if not toks:
        return ""
    # OR the terms, each quoted, so FTS5 operators in user text can't break the query.
    return " OR ".join(f'"{t}"' for t in toks[:20])


def context_search(conn, query: str, limit: int = 5) -> list[dict]:
    """Full-text search via FTS5 (real ranking); falls back to a brute-force
    hybrid scan when FTS5 is unavailable or returns nothing."""
    match = _fts_match(query)
    if match:
        try:
            rows = conn.execute(
                "SELECT source_type, source_id, content, bm25(text_chunks_fts) AS rank "
                "FROM text_chunks_fts WHERE text_chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
            out = []
            for r in rows:
                snippet = r["content"]
                out.append(
                    {
                        "score": round(-(r["rank"] or 0.0), 4),  # bm25: lower is better
                        "source_type": r["source_type"],
                        "source_id": r["source_id"],
                        "snippet": snippet if len(snippet) <= 400 else snippet[:397] + "...",
                    }
                )
            if out:
                return out
        except Exception:
            pass  # FTS5 missing / query error -> fall back to scan

    rows = conn.execute(
        "SELECT source_type, source_id, content FROM text_chunks ORDER BY created_at DESC LIMIT 400"
    ).fetchall()
    scored = [(hybrid_score(query, r["content"]), dict(r)) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, row in scored[:limit]:
        if score <= 0:
            continue
        snippet = row["content"]
        out.append(
            {
                "score": round(score, 4),
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "snippet": snippet if len(snippet) <= 400 else snippet[:397] + "...",
            }
        )
    return out
