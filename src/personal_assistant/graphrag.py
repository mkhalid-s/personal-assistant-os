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


def _expand_work_item_graph(conn: sqlite3.Connection, hit: RetrievalHit) -> list[RetrievalHit]:
    if hit.source_type != "work_item":
        return []
    start = _work_item_node(conn, hit.source_id)
    if start is None:
        return []
    rows = conn.execute(
        """
        SELECT
            e.relation,
            e.weight,
            n.ref_id AS related_id,
            n.label AS related_label
        FROM knowledge_edges e
        JOIN knowledge_nodes n ON n.id = e.to_node_id
        WHERE e.from_node_id = ? AND n.node_type = 'work_item'
        ORDER BY e.weight DESC, n.ref_id ASC
        """,
        (int(start["id"]),),
    ).fetchall()
    expanded: list[RetrievalHit] = []
    for edge in rows:
        chunk = _chunk_for_work_item(conn, int(edge["related_id"]))
        if chunk is None:
            continue
        weight = float(edge["weight"] or 1.0)
        score = hit.score * min(max(weight, 0.0), 1.0) * 0.35
        relation = str(edge["relation"])
        path = (
            f"work_item#{hit.source_id}",
            f"{relation}:{weight:.2f}",
            f"work_item#{int(edge['related_id'])}",
        )
        expanded.append(
            RetrievalHit(
                source_type="work_item",
                source_id=int(edge["related_id"]),
                content=str(chunk["content"]),
                score=score,
                citation=_citation("work_item", int(edge["related_id"])),
                reason=f"graph expansion from {hit.citation} via {relation}",
                graph_path=path,
            )
        )
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
        for hit in direct:
            hits.extend(_expand_work_item_graph(conn, hit))

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
