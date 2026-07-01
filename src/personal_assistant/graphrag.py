from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any

from .retrieval import hybrid_score


@dataclass(frozen=True)
class RetrievalHit:
    source_type: str
    source_id: int
    content: str
    score: float
    citation: str
    reason: str
    graph_path: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "content": self.content,
            "score": round(self.score, 6),
            "citation": self.citation,
            "reason": self.reason,
            "graph_path": list(self.graph_path),
        }


def _citation(source_type: str, source_id: int) -> str:
    return f"{source_type}#{source_id}"


def _work_item_node(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, label
        FROM knowledge_nodes
        WHERE node_type = 'work_item' AND ref_id = ?
        """,
        (int(item_id),),
    ).fetchone()


def _chunk_for_work_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, source_type, source_id, content, provenance_id
        FROM text_chunks
        WHERE source_type = 'work_item' AND source_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (int(item_id),),
    ).fetchone()


def _chunk_for_source(conn: sqlite3.Connection, source_type: str, source_id: str | int) -> sqlite3.Row | None:
    try:
        source_id_int = int(source_id)
    except (TypeError, ValueError):
        return None
    return conn.execute(
        """
        SELECT id, source_type, source_id, content, provenance_id
        FROM text_chunks
        WHERE source_type = ? AND source_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (source_type, source_id_int),
    ).fetchone()


def _direct_hits(conn: sqlite3.Connection, query: str, *, candidate_limit: int) -> list[RetrievalHit]:
    rows = conn.execute(
        """
        SELECT id, source_type, source_id, content, provenance_id
        FROM text_chunks
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (int(candidate_limit),),
    ).fetchall()
    hits: list[RetrievalHit] = []
    for row in rows:
        score = hybrid_score(query, row["content"])
        if score <= 0:
            continue
        source_type = str(row["source_type"])
        source_id = int(row["source_id"])
        provenance = f" provenance#{row['provenance_id']}" if row["provenance_id"] is not None else ""
        hits.append(
            RetrievalHit(
                source_type=source_type,
                source_id=source_id,
                content=str(row["content"]),
                score=score,
                citation=_citation(source_type, source_id),
                reason=f"direct hybrid retrieval{provenance}",
            )
        )
    return hits


def _entity_hits(conn: sqlite3.Connection, query: str) -> list[RetrievalHit]:
    query_lower = query.lower()
    aliases = conn.execute(
        """
        SELECT ea.entity_id, ea.alias, ea.source_type, ea.source_id, ea.confidence, e.canonical_name
        FROM entity_aliases ea
        JOIN entities e ON e.id = ea.entity_id
        WHERE ea.source_type IS NOT NULL AND ea.source_id IS NOT NULL
        ORDER BY ea.confidence DESC, LENGTH(ea.alias) DESC, ea.id ASC
        """
    ).fetchall()
    hits: list[RetrievalHit] = []
    matched_entity_ids: list[int] = []
    for alias in aliases:
        alias_text = str(alias["alias"]).strip()
        if not alias_text or alias_text.lower() not in query_lower:
            continue
        chunk = _chunk_for_source(conn, str(alias["source_type"]), alias["source_id"])
        if chunk is None:
            continue
        matched_entity_ids.append(int(alias["entity_id"]))
        source_type = str(chunk["source_type"])
        source_id = int(chunk["source_id"])
        score = max(hybrid_score(query, str(chunk["content"])), 0.2) + float(alias["confidence"] or 0.0) * 0.1
        hits.append(
            RetrievalHit(
                source_type=source_type,
                source_id=source_id,
                content=str(chunk["content"]),
                score=score,
                citation=_citation(source_type, source_id),
                reason=f"entity alias match: {alias_text}",
            )
        )

    for entity_id in dict.fromkeys(matched_entity_ids):
        related = conn.execute(
            """
            SELECT
                r.relation_type,
                'outbound' AS direction,
                r.confidence AS relationship_confidence,
                ea.source_type,
                ea.source_id,
                ea.confidence AS alias_confidence,
                e.canonical_name
            FROM relationships r
            JOIN entities e ON e.id = r.to_entity_id
            JOIN entity_aliases ea ON ea.entity_id = e.id
            WHERE r.from_entity_id = ? AND ea.source_type IS NOT NULL AND ea.source_id IS NOT NULL
            UNION ALL
            SELECT
                r.relation_type,
                'inbound' AS direction,
                r.confidence AS relationship_confidence,
                ea.source_type,
                ea.source_id,
                ea.confidence AS alias_confidence,
                e.canonical_name
            FROM relationships r
            JOIN entities e ON e.id = r.from_entity_id
            JOIN entity_aliases ea ON ea.entity_id = e.id
            WHERE r.to_entity_id = ? AND ea.source_type IS NOT NULL AND ea.source_id IS NOT NULL
            ORDER BY 3 DESC, 6 DESC, 5 ASC
            """,
            (entity_id, entity_id),
        ).fetchall()
        for row in related:
            chunk = _chunk_for_source(conn, str(row["source_type"]), row["source_id"])
            if chunk is None:
                continue
            source_type = str(chunk["source_type"])
            source_id = int(chunk["source_id"])
            score = max(hybrid_score(query, str(chunk["content"])), 0.15) + float(row["alias_confidence"] or 0.0) * 0.05
            hits.append(
                RetrievalHit(
                    source_type=source_type,
                    source_id=source_id,
                    content=str(chunk["content"]),
                    score=score,
                    citation=_citation(source_type, source_id),
                    reason=f"entity {row['direction']} relationship expansion via {row['relation_type']}",
                    graph_path=(
                        f"entity#{entity_id}",
                        f"{row['direction']}:{row['relation_type']}",
                        str(row["canonical_name"]),
                    ),
                )
            )
    return hits


def _claim_hits(conn: sqlite3.Connection, query: str) -> list[RetrievalHit]:
    rows = conn.execute(
        """
        SELECT id, claim_text, source_type, source_id, confidence
        FROM claims
        ORDER BY confidence DESC, id DESC
        LIMIT 200
        """
    ).fetchall()
    hits: list[RetrievalHit] = []
    for row in rows:
        score = hybrid_score(query, str(row["claim_text"]))
        if score <= 0:
            continue
        source_type = str(row["source_type"])
        source_id_raw = row["source_id"]
        try:
            source_id = int(source_id_raw)
        except (TypeError, ValueError):
            source_type = "claim"
            source_id = int(row["id"])
        chunk = _chunk_for_source(conn, source_type, source_id) if source_type != "claim" else None
        content = str(chunk["content"]) if chunk else str(row["claim_text"])
        hits.append(
            RetrievalHit(
                source_type=source_type,
                source_id=source_id,
                content=content,
                score=score + float(row["confidence"] or 0.0) * 0.12,
                citation=_citation(source_type, source_id),
                reason=f"claim-backed retrieval: claim#{row['id']}",
                graph_path=(f"claim#{row['id']}", f"{source_type}#{source_id}"),
            )
        )
    return hits


def _expand_work_item_graph(conn: sqlite3.Connection, hit: RetrievalHit, *, max_hops: int = 1) -> list[RetrievalHit]:
    if hit.source_type != "work_item":
        return []
    start = _work_item_node(conn, hit.source_id)
    if start is None:
        return []
    max_hops = max(1, min(int(max_hops), 4))
    expanded: list[RetrievalHit] = []
    queue: list[tuple[int, int, float, tuple[str, ...], set[int]]] = [
        (int(start["id"]), 0, 1.0, (f"work_item#{hit.source_id}",), {int(start["id"])})
    ]
    while queue:
        node_id, depth, path_weight, path, seen = queue.pop(0)
        if depth >= max_hops:
            continue
        rows = conn.execute(
            """
            SELECT
                e.relation,
                e.weight,
                n.id AS node_id,
                n.ref_id AS related_id,
                n.label AS related_label
            FROM knowledge_edges e
            JOIN knowledge_nodes n ON n.id = e.to_node_id
            WHERE e.from_node_id = ? AND n.node_type = 'work_item'
            ORDER BY e.weight DESC, n.ref_id ASC
            """,
            (int(node_id),),
        ).fetchall()
        for edge in rows:
            next_node_id = int(edge["node_id"])
            if next_node_id in seen:
                continue
            related_id = int(edge["related_id"])
            chunk = _chunk_for_work_item(conn, related_id)
            if chunk is None:
                continue
            weight = min(max(float(edge["weight"] or 1.0), 0.0), 1.0)
            relation = str(edge["relation"])
            next_weight = path_weight * weight
            next_path = path + (f"{relation}:{weight:.2f}", f"work_item#{related_id}")
            hop_count = depth + 1
            score = hit.score * next_weight * (0.35 / hop_count)
            reason = (
                f"graph expansion from {hit.citation} via {relation}"
                if hop_count == 1
                else f"multi-hop graph expansion from {hit.citation} hops={hop_count} via {relation}"
            )
            expanded.append(
                RetrievalHit(
                    source_type="work_item",
                    source_id=related_id,
                    content=str(chunk["content"]),
                    score=score,
                    citation=_citation("work_item", related_id),
                    reason=reason,
                    graph_path=next_path,
                )
            )
            queue.append((next_node_id, hop_count, next_weight, next_path, seen | {next_node_id}))
    return expanded


def _record_retrieval_run(
    conn: sqlite3.Connection,
    query: str,
    hits: list[dict[str, Any]],
    *,
    mode: str,
    limit: int,
    graph_hops: int,
    candidate_limit: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO retrieval_runs (
            query, mode, limit_requested, graph_hops, candidate_limit, selected_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (query, mode, int(limit), int(graph_hops), int(candidate_limit), len(hits)),
    )
    run_id = int(cur.lastrowid)
    for rank, hit in enumerate(hits, start=1):
        preview = str(hit["content"]).strip().replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:237] + "..."
        conn.execute(
            """
            INSERT INTO retrieval_run_sources (
                retrieval_run_id, rank, source_type, source_id, citation, score,
                reason, graph_path_json, content_preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                rank,
                hit["source_type"],
                int(hit["source_id"]),
                hit["citation"],
                float(hit["score"]),
                hit["reason"],
                json.dumps(hit["graph_path"], ensure_ascii=True),
                preview,
            ),
        )
    return run_id


def retrieve(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    graph_hops: int = 1,
    candidate_limit: int = 400,
    record_run: bool = False,
    mode: str = "graph",
) -> list[dict[str, Any]]:
    """SQLite-first GraphRAG retrieval trace over existing chunks and graph edges.

    This intentionally does not add graph storage, vector indexes, or provider calls. It
    defines the behavior later GraphRAG storage must preserve: every returned source has
    a citation, and graph-expanded sources explain the relationship path that selected
    them.
    """
    if not query.strip() or limit <= 0:
        return []
    direct = _direct_hits(conn, query, candidate_limit=candidate_limit)
    hits = list(direct)
    if graph_hops > 0:
        hits.extend(_entity_hits(conn, query))
        hits.extend(_claim_hits(conn, query))
        for hit in direct:
            hits.extend(_expand_work_item_graph(conn, hit, max_hops=graph_hops))

    best_by_source: dict[tuple[str, int], RetrievalHit] = {}
    for hit in hits:
        key = (hit.source_type, hit.source_id)
        current = best_by_source.get(key)
        if current is None or hit.score > current.score or (hit.graph_path and not current.graph_path):
            best_by_source[key] = hit

    ordered = sorted(
        best_by_source.values(),
        key=lambda h: (h.score, 1 if h.graph_path else 0, h.source_type, h.source_id),
        reverse=True,
    )
    result = [hit.as_dict() for hit in ordered[:limit]]
    if record_run:
        run_id = _record_retrieval_run(
            conn,
            query,
            result,
            mode=mode,
            limit=limit,
            graph_hops=graph_hops,
            candidate_limit=candidate_limit,
        )
        for rank, hit in enumerate(result, start=1):
            hit["retrieval_run_id"] = run_id
            hit["retrieval_rank"] = rank
    return result
