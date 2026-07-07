from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .privacy import apply_privacy_filters


@dataclass(frozen=True)
class EntityCandidate:
    entity_type: str
    canonical_name: str
    aliases: tuple[str, ...]
    confidence: float

    def as_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "confidence": self.confidence,
        }


_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,12}-\d{1,8})\b")
_PR_RE = re.compile(r"(?i)\b(?:pr|pull request)\s*#?(\d{1,8})\b")
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_REPO_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9_.-]{2,39}/[A-Za-z0-9_.-]{2,100})(?![\w.-])")
_HANDLE_RE = re.compile(r"(?<![\w.-])@([A-Za-z0-9][A-Za-z0-9_.-]{1,38})\b")
_LABELED_RE = re.compile(
    r"\b((?i:project|service|system|api|document|doc))\s+([A-Z][A-Za-z0-9]*(?:[ -][A-Z][A-Za-z0-9]*){0,4})"
)

_TYPE_MAP = {
    "project": "project",
    "service": "service",
    "system": "system",
    "api": "api",
    "document": "document",
    "doc": "document",
}


def _clean_alias(value: str) -> str:
    return value.strip().strip(".,;:!?)]}>")


def _canonical_url(value: str) -> str:
    cleaned = _clean_alias(value)
    parts = urlsplit(cleaned)
    path = parts.path.rstrip("/") or parts.path
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _canonical_label(kind: str, value: str) -> str:
    label = " ".join(_clean_alias(value).split())
    prefix = "API" if kind.lower() == "api" else kind.capitalize()
    if label.lower().startswith(prefix.lower() + " "):
        return label
    return f"{prefix} {label}"


def _add_candidate(
    candidates: list[EntityCandidate],
    seen: set[tuple[str, str]],
    entity_type: str,
    canonical_name: str,
    aliases: tuple[str, ...],
    confidence: float,
) -> None:
    canonical_name = _clean_alias(canonical_name)
    if not canonical_name:
        return
    key = (entity_type, canonical_name.lower())
    if key in seen:
        return
    seen.add(key)
    clean_aliases = tuple(dict.fromkeys(_clean_alias(a) for a in aliases if _clean_alias(a)))
    candidates.append(EntityCandidate(entity_type, canonical_name, clean_aliases or (canonical_name,), confidence))


def extract_entities(text: str) -> list[dict]:
    """Extract conservative, deterministic entity candidates from local text.

    This is intentionally rule-based and high precision. It avoids broad proper-noun
    extraction so the local assistant does not pollute the graph with every title-cased
    phrase before a stronger entity model exists.
    """
    candidates: list[EntityCandidate] = []
    seen: set[tuple[str, str]] = set()

    for match in _TICKET_RE.finditer(text):
        value = match.group(1).upper()
        _add_candidate(candidates, seen, "ticket", value, (match.group(1),), 0.95)

    for match in _PR_RE.finditer(text):
        number = match.group(1)
        _add_candidate(candidates, seen, "pull_request", f"PR #{number}", (match.group(0), f"#{number}"), 0.9)

    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        canonical = _canonical_url(raw)
        if canonical:
            _add_candidate(candidates, seen, "document", canonical, (raw,), 0.85)

    text_without_urls = _URL_RE.sub(" ", text)
    for match in _REPO_RE.finditer(text_without_urls):
        value = _clean_alias(match.group(1))
        if "/" in value and not value.startswith(("http/", "https/")):
            _add_candidate(candidates, seen, "repository", value.lower(), (value,), 0.85)

    for match in _HANDLE_RE.finditer(text_without_urls):
        handle = "@" + match.group(1)
        _add_candidate(candidates, seen, "person", handle.lower(), (handle,), 0.75)

    for match in _LABELED_RE.finditer(text_without_urls):
        raw_kind = match.group(1).lower()
        entity_type = _TYPE_MAP[raw_kind]
        canonical = _canonical_label(raw_kind, match.group(2))
        _add_candidate(candidates, seen, entity_type, canonical, (match.group(0), canonical), 0.8)

    return [candidate.as_dict() for candidate in candidates]


def upsert_entity(conn: sqlite3.Connection, entity_type: str, canonical_name: str) -> int:
    conn.execute(
        """
        INSERT INTO entities (entity_type, canonical_name, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(entity_type, canonical_name) DO UPDATE SET updated_at=CURRENT_TIMESTAMP
        """,
        (entity_type, canonical_name),
    )
    row = conn.execute(
        "SELECT id FROM entities WHERE entity_type = ? AND canonical_name = ?",
        (entity_type, canonical_name),
    ).fetchone()
    return int(row["id"])


def add_alias(
    conn: sqlite3.Connection,
    entity_id: int,
    alias: str,
    *,
    source_type: str = "",
    source_id: str | int | None = None,
    confidence: float = 0.8,
) -> None:
    alias = _clean_alias(alias)
    if not alias:
        return
    conn.execute(
        """
        INSERT INTO entity_aliases (entity_id, alias, source_type, source_id, confidence)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(entity_id, alias) DO UPDATE SET
            source_type=COALESCE(excluded.source_type, entity_aliases.source_type),
            source_id=COALESCE(excluded.source_id, entity_aliases.source_id),
            confidence=MAX(entity_aliases.confidence, excluded.confidence)
        """,
        (
            int(entity_id),
            alias,
            source_type or None,
            str(source_id) if source_id is not None else None,
            float(confidence),
        ),
    )


def record_entities(
    conn: sqlite3.Connection,
    text: str,
    *,
    source_type: str = "note",
    source_id: str | int | None = None,
) -> list[dict]:
    safe_text = apply_privacy_filters(conn, text or "")
    recorded: list[dict] = []
    for candidate in extract_entities(safe_text):
        entity_id = upsert_entity(conn, candidate["entity_type"], candidate["canonical_name"])
        for alias in candidate["aliases"]:
            add_alias(
                conn,
                entity_id,
                alias,
                source_type=source_type,
                source_id=source_id,
                confidence=float(candidate["confidence"]),
            )
        recorded.append({"id": entity_id, **candidate})
    return recorded


def list_entities(conn: sqlite3.Connection, *, entity_type: str = "", limit: int = 50) -> list[dict]:
    if entity_type:
        rows = conn.execute(
            """
            SELECT id, entity_type, canonical_name, created_at, updated_at
            FROM entities
            WHERE entity_type = ?
            ORDER BY entity_type ASC, canonical_name ASC
            LIMIT ?
            """,
            (entity_type, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, entity_type, canonical_name, created_at, updated_at
            FROM entities
            ORDER BY entity_type ASC, canonical_name ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        aliases = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias ASC",
            (int(row["id"]),),
        ).fetchall()
        item = dict(row)
        item["aliases"] = [str(a["alias"]) for a in aliases]
        result.append(item)
    return result
