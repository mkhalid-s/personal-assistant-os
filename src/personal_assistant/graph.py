from __future__ import annotations

import sqlite3


def upsert_node(conn: sqlite3.Connection, node_type: str, ref_id: int, label: str) -> int:
    row = conn.execute(
        "SELECT id FROM knowledge_nodes WHERE node_type = ? AND ref_id = ?",
        (node_type, ref_id),
    ).fetchone()
    if row:
        return int(row["id"])
    conn.execute(
        "INSERT INTO knowledge_nodes (node_type, ref_id, label) VALUES (?, ?, ?)",
        (node_type, ref_id, label),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def connect_work_items(
    conn: sqlite3.Connection,
    from_item_id: int,
    to_item_id: int,
    relation: str,
    weight: float = 1.0,
) -> None:
    from_node = upsert_node(conn, "work_item", from_item_id, f"work_item:{from_item_id}")
    to_node = upsert_node(conn, "work_item", to_item_id, f"work_item:{to_item_id}")
    conn.execute(
        """
        INSERT INTO knowledge_edges (from_node_id, to_node_id, relation, weight, source)
        VALUES (?, ?, ?, ?, 'manual')
        """,
        (from_node, to_node, relation, weight),
    )
