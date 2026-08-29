"""Session transcript → digest → re-index loop (X3, inspired by Spotify Xirp).

After each autonomy cycle, distill what the agent observed into a short digest
that gets indexed in FTS5 + embedding store so future cycles can semantically
recall past decisions — the core of the P5 learning loop.

The digest flow:
  1. Pull agent_observations for the completed task
  2. Call the provider to summarise them in 3-5 sentences
  3. Write to assistant_digests (source_type='autonomy_task', source_id=task_id)
  4. Call agentcore.remember() → FTS5 + embedding indexed for free via B5 hooks
"""
from __future__ import annotations

import sqlite3

from .agentcore import remember
from .privacy import apply_privacy_filters


def generate_cycle_digest(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    backend_name: str = "claude",
    max_observations: int = 40,
) -> str:
    """Call the provider to generate a digest of an autonomy cycle.

    Pulls agent_observations for task_id (most recent max_observations),
    builds a summarisation prompt, and returns the provider's reply text.
    Returns an empty string when there are no observations or the provider fails.
    """
    obs = conn.execute(
        """
        SELECT observation_type, content
        FROM agent_observations
        WHERE agent_task_id = ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (int(task_id), int(max_observations)),
    ).fetchall()
    if not obs:
        return ""

    obs_text = "\n".join(f"[{o['observation_type']}] {o['content'][:300]}" for o in obs)
    try:
        from . import providers
        backend = providers.get_backend(backend_name)
        ok, _ = backend.available()
        if not ok:
            return ""
        result = backend.reason(
            conn,
            {
                "purpose": "digest",
                "objective": (
                    "Summarise this autonomy cycle in 3-5 sentences. "
                    "State: what goal was pursued, what actions were proposed or executed, "
                    "what succeeded or was blocked, and what the agent should remember for next time."
                ),
                "context": obs_text,
                "analogies": [],
            },
        )
        reply = str(result.get("reply") or "").strip()
        return reply[:1500]
    except Exception:  # noqa: BLE001
        return ""


def record_digest(
    conn: sqlite3.Connection,
    task_id: int,
    title: str,
    body: str,
    *,
    source_type: str = "autonomy_task",
) -> int | None:
    """Persist a digest and index it for retrieval.

    Writes to assistant_digests then calls agentcore.remember() so the digest
    body is routed through FTS5 indexing and (when fastembed is installed) the
    embedding cache — making it semantically searchable in future cycles.

    Returns the digest id, or None if the body is empty after privacy filtering.
    """
    body = apply_privacy_filters(conn, body.strip())
    if not body:
        return None

    title = apply_privacy_filters(conn, title.strip())[:500] or f"Cycle digest (task #{task_id})"

    cur = conn.execute(
        """
        INSERT INTO assistant_digests (source_type, source_id, title, body, payload_json)
        VALUES (?, ?, ?, ?, '{}')
        """,
        (source_type, int(task_id), title, body),
    )
    digest_id = cur.lastrowid
    assert digest_id is not None
    remember(conn, body, source_type="digest", source_id=int(digest_id))
    return int(digest_id)


def maybe_generate_and_record(
    conn: sqlite3.Connection,
    task_id: int,
    task_objective: str,
    *,
    backend_name: str = "claude",
) -> int | None:
    """Generate and record a digest for a completed cycle, or skip silently.

    Designed for use at the end of _finish_cycle — never raises, never blocks
    the cycle result. Returns the digest id or None.
    """
    try:
        body = generate_cycle_digest(conn, task_id, backend_name=backend_name)
        if not body:
            return None
        title = f"Cycle: {task_objective[:80]}"
        return record_digest(conn, task_id, title, body)
    except Exception:  # noqa: BLE001
        return None


def list_digests(conn: sqlite3.Connection, *, limit: int = 20, source_type: str = "") -> list[dict]:
    """Return recent digests, optionally filtered by source_type."""
    if source_type:
        rows = conn.execute(
            """
            SELECT id, source_type, source_id, title, body, created_at
            FROM assistant_digests
            WHERE source_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (source_type, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, source_type, source_id, title, body, created_at
            FROM assistant_digests
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]
