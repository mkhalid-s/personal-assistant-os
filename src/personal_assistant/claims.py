from __future__ import annotations

import re
import sqlite3

from .privacy import apply_privacy_filters

_SENTENCE_RE = re.compile(r"[^.!?\n]+")
_CLAIM_CUES = (
    " is ", " are ", " has ", " have ", " needs ", " requires ",
    " blocks ", " depends on ", " mitigates ", " supports ", " confirms ",
)


def extract_claims(text: str) -> list[dict]:
    claims: list[dict] = []
    seen: set[str] = set()
    for match in _SENTENCE_RE.finditer(text or ""):
        sentence = " ".join(match.group(0).strip().split())
        if len(sentence) < 12 or len(sentence) > 300:
            continue
        lower = f" {sentence.lower()} "
        if not any(cue in lower for cue in _CLAIM_CUES):
            continue
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        claims.append({"claim_text": sentence, "confidence": 0.72})
    return claims


def record_claims(
    conn: sqlite3.Connection,
    text: str,
    *,
    source_type: str = "note",
    source_id: str | int | None = None,
) -> list[dict]:
    safe_text = apply_privacy_filters(conn, text or "")
    recorded: list[dict] = []
    source_type_value = source_type or "note"
    source_id_value = str(source_id) if source_id is not None else ""
    for claim in extract_claims(safe_text):
        conn.execute(
            """
            INSERT INTO claims (claim_text, source_type, source_id, confidence)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(claim_text, source_type, source_id) DO UPDATE SET
                confidence=MAX(claims.confidence, excluded.confidence)
            """,
            (claim["claim_text"], source_type_value, source_id_value, float(claim["confidence"])),
        )
        row = conn.execute(
            """
            SELECT id FROM claims
            WHERE claim_text = ? AND source_type = ? AND COALESCE(source_id, '') = COALESCE(?, '')
            """,
            (claim["claim_text"], source_type_value, source_id_value),
        ).fetchone()
        recorded.append({"id": int(row["id"]), **claim, "source_type": source_type_value, "source_id": source_id_value})
    return recorded


def list_claims(conn: sqlite3.Connection, *, source_type: str = "", limit: int = 50) -> list[dict]:
    if source_type:
        rows = conn.execute(
            """
            SELECT id, claim_text, source_type, source_id, confidence, created_at
            FROM claims
            WHERE source_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (source_type, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, claim_text, source_type, source_id, confidence, created_at
            FROM claims
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]
