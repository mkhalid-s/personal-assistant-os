"""Personal service catalog for MYOS.

Stores services as knowledge_nodes (node_type='service') and their dependency
relationships as knowledge_edges, reusing the existing graph infrastructure so
graphrag's traversal works for free.

Usage:
    catalog.add_service(conn, "auth-service", owner="platform", description="OAuth2 gateway", deps=["postgres"])
    context = catalog.get_service_context(conn, "auth token expired")
"""
from __future__ import annotations

import sqlite3
import zlib

from .graph import upsert_node
from .inbox import index_chunk
from .retrieval import hybrid_score


def _service_ref_id(name: str) -> int:
    """Stable integer ref_id for a service name (positive, fits SQLite INTEGER).

    Uses zlib.crc32 — NOT hash() — because Python's hash() is randomized per
    process (PYTHONHASHSEED). A ref_id written by one myos invocation must be
    readable by the next invocation, so it must be deterministic across processes.
    """
    return zlib.crc32(name.lower().strip().encode("utf-8")) % (10**9)


def add_service(
    conn: sqlite3.Connection,
    name: str,
    *,
    owner: str = "",
    description: str = "",
    deps: list[str] | None = None,
) -> int:
    """Add or update a service in the catalog.

    Creates a knowledge_node (node_type='service') and knowledge_edges for each
    dependency. Also indexes a text_chunk so the service is searchable via FTS5
    and semantic retrieval.

    Returns the knowledge_node id.
    """
    name = name.strip()
    if not name:
        raise ValueError("service name cannot be empty")
    ref_id = _service_ref_id(name)
    node_id = upsert_node(conn, "service", ref_id, name)

    content = f"Service: {name}."
    if owner:
        content += f" Owner: {owner}."
    if description:
        content += f" {description.strip()}."
    index_chunk(conn, "service", ref_id, content)

    for dep in (deps or []):
        dep = dep.strip()
        if not dep:
            continue
        dep_ref = _service_ref_id(dep)
        dep_node = upsert_node(conn, "service", dep_ref, dep)
        conn.execute(
            """
            INSERT INTO knowledge_edges (from_node_id, to_node_id, relation, weight, source)
            VALUES (?, ?, 'depends_on', 0.9, 'catalog')
            ON CONFLICT DO NOTHING
            """,
            (node_id, dep_node),
        )
    return node_id


def remove_service(conn: sqlite3.Connection, name: str) -> bool:
    """Remove a service node and its edges. Returns True if found and removed."""
    ref_id = _service_ref_id(name.strip())
    row = conn.execute(
        "SELECT id FROM knowledge_nodes WHERE node_type='service' AND ref_id=?",
        (ref_id,),
    ).fetchone()
    if not row:
        return False
    node_id = row["id"]
    conn.execute("DELETE FROM knowledge_edges WHERE from_node_id=? OR to_node_id=?", (node_id, node_id))
    conn.execute("DELETE FROM knowledge_nodes WHERE id=?", (node_id,))
    conn.execute("DELETE FROM text_chunks WHERE source_type='service' AND source_id=?", (ref_id,))
    return True


def list_services(conn: sqlite3.Connection) -> list[dict]:
    """Return all catalog services with their direct dependencies."""
    nodes = conn.execute(
        "SELECT id, ref_id, label FROM knowledge_nodes WHERE node_type='service' ORDER BY label ASC"
    ).fetchall()
    result: list[dict] = []
    for node in nodes:
        deps = conn.execute(
            """
            SELECT n.label FROM knowledge_edges e
            JOIN knowledge_nodes n ON n.id = e.to_node_id
            WHERE e.from_node_id = ? AND e.relation = 'depends_on'
            ORDER BY n.label ASC
            """,
            (node["id"],),
        ).fetchall()
        chunk = conn.execute(
            "SELECT content FROM text_chunks WHERE source_type='service' AND source_id=? LIMIT 1",
            (node["ref_id"],),
        ).fetchone()
        result.append({
            "name": node["label"],
            "description": str(chunk["content"]) if chunk else "",
            "deps": [d["label"] for d in deps],
        })
    return result


def get_service_context(conn: sqlite3.Connection, query: str, *, limit: int = 5) -> str:
    """Return a formatted context string of the most relevant catalog services.

    Scores all service nodes by hybrid_score against the query, returns the
    top-k with their direct dependencies formatted for prompt injection.
    Returns an empty string when the catalog is empty (no-op for callers).
    """
    nodes = conn.execute(
        "SELECT id, ref_id, label FROM knowledge_nodes WHERE node_type='service'"
    ).fetchall()
    if not nodes:
        return ""

    scored: list[tuple[float, dict]] = []
    for node in nodes:
        # Score against full chunk content (includes owner + description) so
        # "authentication" matches "auth-service / OAuth2 authentication gateway".
        chunk = conn.execute(
            "SELECT content FROM text_chunks WHERE source_type='service' AND source_id=? LIMIT 1",
            (node["ref_id"],),
        ).fetchone()
        text = str(chunk["content"]) if chunk else str(node["label"])
        score = hybrid_score(query, text)
        if score > 0:
            scored.append((score, node))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [item for _, item in scored[:limit]]

    lines: list[str] = ["Relevant services:"]
    for node in top:
        deps = conn.execute(
            """
            SELECT n.label FROM knowledge_edges e
            JOIN knowledge_nodes n ON n.id = e.to_node_id
            WHERE e.from_node_id = ? AND e.relation = 'depends_on'
            ORDER BY n.label ASC
            """,
            (node["id"],),
        ).fetchall()
        dep_str = ", ".join(d["label"] for d in deps)
        chunk = conn.execute(
            "SELECT content FROM text_chunks WHERE source_type='service' AND source_id=? LIMIT 1",
            (node["ref_id"],),
        ).fetchone()
        desc = str(chunk["content"]) if chunk else node["label"]
        line = f"  - {desc}"
        if dep_str:
            line += f" [depends on: {dep_str}]"
        lines.append(line)

    return "\n".join(lines)
