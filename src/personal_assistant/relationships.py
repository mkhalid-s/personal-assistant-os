from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import entities
from .privacy import apply_privacy_filters


@dataclass(frozen=True)
class RelationshipCandidate:
    from_entity: dict[str, Any]
    to_entity: dict[str, Any]
    relation_type: str
    evidence: str
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_entity": self.from_entity,
            "to_entity": self.to_entity,
            "relation_type": self.relation_type,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    entity: dict[str, Any]
    alias: str


_SENTENCE_RE = re.compile(r"[^.!?\n]+")
_RELATION_PHRASES: tuple[tuple[str, str, bool], ...] = (
    ("depends on", "depends_on", False),
    ("requires", "depends_on", False),
    ("owns", "owns", False),
    ("owned by", "owns", True),
    ("blocks", "blocks", False),
    ("blocked by", "blocks", True),
    ("mitigates", "mitigates", False),
    ("mitigated by", "mitigates", True),
    ("references", "references", False),
    ("relates to", "relates_to", False),
)


def _find_mentions(sentence: str, extracted: list[dict[str, Any]]) -> list[_Mention]:
    mentions: list[_Mention] = []
    lower = sentence.lower()
    seen: set[tuple[str, str]] = set()
    for entity in extracted:
        aliases = sorted(entity.get("aliases", []), key=len, reverse=True)
        for alias in aliases:
            alias_text = str(alias).strip()
            if not alias_text:
                continue
            idx = lower.find(alias_text.lower())
            if idx < 0:
                continue
            key = (str(entity["entity_type"]), str(entity["canonical_name"]).lower())
            if key in seen:
                continue
            seen.add(key)
            mentions.append(_Mention(idx, idx + len(alias_text), entity, alias_text))
            break
    return sorted(mentions, key=lambda m: (m.start, m.end))


def _relation_between(segment: str) -> tuple[str, bool] | None:
    normalized = " ".join(segment.lower().split())
    if len(normalized) > 80:
        return None
    for phrase, relation_type, inverse in _RELATION_PHRASES:
        if phrase in normalized:
            return relation_type, inverse
    return None


def extract_relationships(text: str) -> list[dict[str, Any]]:
    """Extract high-confidence typed relationships between deterministic entities.

    This first version is deliberately narrow: it only records relationships where two
    recognized entities appear in the same sentence with an explicit relation phrase
    between them. Broader relationship inference belongs behind later reviewable gates.
    """
    extracted = entities.extract_entities(text)
    if len(extracted) < 2:
        return []

    candidates: list[RelationshipCandidate] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for sentence_match in _SENTENCE_RE.finditer(text):
        sentence = sentence_match.group(0).strip()
        if not sentence:
            continue
        mentions = _find_mentions(sentence, extracted)
        if len(mentions) < 2:
            continue
        for idx, left in enumerate(mentions):
            for right in mentions[idx + 1 :]:
                rel = _relation_between(sentence[left.end : right.start])
                if rel is None:
                    continue
                relation_type, inverse = rel
                src = right if inverse else left
                dst = left if inverse else right
                key = (
                    str(src.entity["entity_type"]),
                    str(src.entity["canonical_name"]).lower(),
                    relation_type,
                    str(dst.entity["entity_type"]),
                    str(dst.entity["canonical_name"]).lower(),
                )
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    RelationshipCandidate(
                        from_entity=src.entity,
                        to_entity=dst.entity,
                        relation_type=relation_type,
                        evidence=sentence,
                        confidence=0.82,
                    )
                )
    return [candidate.as_dict() for candidate in candidates]


def _entity_id_by_candidate(conn: sqlite3.Connection, candidate: dict[str, Any]) -> int:
    return entities.upsert_entity(conn, str(candidate["entity_type"]), str(candidate["canonical_name"]))


def record_relationships(
    conn: sqlite3.Connection,
    text: str,
    *,
    source_type: str = "note",
    source_id: str | int | None = None,
) -> list[dict[str, Any]]:
    safe_text = apply_privacy_filters(conn, text or "")
    source_type_value = source_type or "note"
    source_id_value = str(source_id) if source_id is not None else ""
    # Persist entities first so relationships always point at canonical rows.
    entities.record_entities(conn, safe_text, source_type=source_type_value, source_id=source_id_value)
    recorded: list[dict[str, Any]] = []
    for candidate in extract_relationships(safe_text):
        from_id = _entity_id_by_candidate(conn, candidate["from_entity"])
        to_id = _entity_id_by_candidate(conn, candidate["to_entity"])
        conn.execute(
            """
            INSERT INTO relationships (
                from_entity_id, to_entity_id, relation_type, source_type, source_id, evidence, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_entity_id, to_entity_id, relation_type, source_type, source_id) DO UPDATE SET
                evidence=excluded.evidence,
                confidence=MAX(relationships.confidence, excluded.confidence)
            """,
            (
                from_id,
                to_id,
                candidate["relation_type"],
                source_type_value,
                source_id_value,
                candidate["evidence"],
                float(candidate["confidence"]),
            ),
        )
        row = conn.execute(
            """
            SELECT id FROM relationships
            WHERE from_entity_id=? AND to_entity_id=? AND relation_type=?
              AND COALESCE(source_type, '') = COALESCE(?, '')
              AND COALESCE(source_id, '') = COALESCE(?, '')
            """,
            (from_id, to_id, candidate["relation_type"], source_type_value, source_id_value),
        ).fetchone()
        recorded.append({"id": int(row["id"]), "from_entity_id": from_id, "to_entity_id": to_id, **candidate})
    return recorded


def list_relationships(conn: sqlite3.Connection, *, relation_type: str = "", limit: int = 50) -> list[dict[str, Any]]:
    params: tuple[Any, ...]
    where = ""
    if relation_type:
        where = "WHERE r.relation_type = ?"
        params = (relation_type, int(limit))
    else:
        params = (int(limit),)
    rows = conn.execute(
        f"""
        SELECT
            r.id,
            r.relation_type,
            r.evidence,
            r.confidence,
            r.source_type,
            r.source_id,
            fe.entity_type AS from_type,
            fe.canonical_name AS from_name,
            te.entity_type AS to_type,
            te.canonical_name AS to_name
        FROM relationships r
        JOIN entities fe ON fe.id = r.from_entity_id
        JOIN entities te ON te.id = r.to_entity_id
        {where}
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]
