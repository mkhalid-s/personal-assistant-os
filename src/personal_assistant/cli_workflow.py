from __future__ import annotations

import argparse
import json
from collections.abc import Callable

from .connectors import AhaConnector, ConfluenceConnector, GitHubConnector, JiraConnector
from .db import append_event, get_connection
from .extraction import extract_suggestions
from .inbox import (
    ensure_work_item_node,
    index_chunk,
    infer_from_external,
    infer_kind,
    infer_priority,
    infer_risk,
    insert_inbox_item_dedup,
)
from .privacy import apply_privacy_filters
from .pulse import detect_mode


def cmd_capture(args: argparse.Namespace) -> None:
    conn = get_connection()
    # Redact at the boundary: this raw text lands in inbox_items and is later indexed
    # verbatim into the FTS-backed text_chunks at triage time (finding #8). Infer the kind
    # from the redacted text; redaction labels don't disturb keyword inference.
    text = apply_privacy_filters(conn, args.text).strip()
    kind = args.kind if args.kind else infer_kind(text)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO inbox_items (text, kind, owner, due_date, source, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (text, kind, args.owner, args.due, "manual", 0.95),
    )
    if cur.rowcount == 0:
        conn.commit()
        print(f"Duplicate capture ignored: [{kind}] {text}")
        return
    inbox_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        conn,
        "capture",
        "inbox_item",
        inbox_id,
        json.dumps({"kind": kind, "text": text}, ensure_ascii=True),
    )
    conn.commit()
    print(f"Captured: [{kind}] {text}")


def cmd_triage(_: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM inbox_items WHERE status = 'new' ORDER BY created_at ASC").fetchall()

    if not rows:
        print("No new inbox items to triage.")
        return

    count = 0
    for row in rows:
        text = row["text"].strip()
        kind = row["kind"] if row["kind"] != "note" else infer_kind(text)
        priority = infer_priority(text, row["due_date"])
        risk_score = infer_risk(text, row["due_date"])
        title = text if len(text) <= 90 else text[:87] + "..."

        conn.execute(
            """
            INSERT INTO work_items (inbox_id, title, kind, priority, risk_score, owner, due_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (row["id"], title, kind, priority, risk_score, row["owner"], row["due_date"]),
        )
        item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("inbox_item", str(row["id"]), "heuristic:triage", "1", 0.72, text[:400]),
        )
        provenance_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        ensure_work_item_node(conn, item_id, title)
        index_chunk(conn, "work_item", item_id, text, provenance_id=provenance_id)
        if kind == "commitment":
            conn.execute(
                """
                INSERT INTO commitment_log (work_item_id, promised_on, due_on, outcome)
                VALUES (?, CURRENT_TIMESTAMP, ?, 'open')
                """,
                (item_id, row["due_date"]),
            )
        conn.execute(
            "UPDATE inbox_items SET status = 'triaged', triaged_at = CURRENT_TIMESTAMP WHERE id = ?",
            (row["id"],),
        )
        append_event(
            conn,
            "triage",
            "work_item",
            item_id,
            json.dumps({"kind": kind, "priority": priority, "risk": risk_score}, ensure_ascii=True),
        )
        count += 1

    conn.commit()
    print(f"Triaged {count} inbox items into actionable work items.")


def cmd_today(args: argparse.Namespace) -> None:
    meeting_hours = args.meeting_hours
    mode = detect_mode(meeting_hours)

    conn = get_connection()
    top_items = conn.execute(
        """
        SELECT * FROM work_items
        WHERE status = 'open'
        ORDER BY priority ASC, risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 5
        """
    ).fetchall()

    risky = conn.execute(
        """
        SELECT * FROM work_items
        WHERE status = 'open' AND risk_score >= 60
        ORDER BY risk_score DESC
        LIMIT 3
        """
    ).fetchall()

    print(f"Mode: {mode} (meeting hours: {meeting_hours})")
    print("\nTop outcomes today:")
    if not top_items:
        print("- No open work items. Run 'myos capture' then 'myos triage'.")
    else:
        for i, item in enumerate(top_items[:3], start=1):
            due = item["due_date"] or "no due date"
            print(f"{i}. {item['title']} | kind={item['kind']} | due={due} | risk={item['risk_score']}")

    print("\nRisk watch:")
    if not risky:
        print("- No high-risk items right now.")
    else:
        for item in risky:
            due = item["due_date"] or "no due date"
            print(f"- {item['title']} (risk={item['risk_score']}, due={due})")

    if mode == "meeting-heavy":
        print("\nMeeting-heavy guidance:")
        print("- Focus on coordination and commitments, not deep execution.")
        print("- Keep only 1-2 tiny execution goals for today.")
        print("- Proactively delegate or renegotiate at-risk commitments.")


def cmd_risk_radar(_: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM work_items
        WHERE status = 'open'
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 15
        """
    ).fetchall()

    if not rows:
        print("No open work items.")
        return

    print("Risk radar (top open items):")
    for item in rows:
        due = item["due_date"] or "no due date"
        flag = "!" if item["risk_score"] >= 60 else "-"
        print(f"{flag} {item['title']} | risk={item['risk_score']} | priority={item['priority']} | due={due}")


def cmd_sync(args: argparse.Namespace, load_env_file: Callable[[str], int]) -> None:
    if args.env_file:
        loaded = load_env_file(args.env_file)
        print(f"Loaded {loaded} vars from {args.env_file}")
    conn = get_connection()
    mapping = {
        "jira": JiraConnector,
        "github": GitHubConnector,
        "confluence": ConfluenceConnector,
        "aha": AhaConnector,
    }
    targets = [args.connector] if args.connector != "all" else list(mapping.keys())
    for target in targets:
        result = mapping[target](conn).sync()
        print(f"{result.connector}: status={result.status}, fetched={result.fetched}, msg={result.message}")


def cmd_ingest_external(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT e.id, e.connector, e.item_type, e.title, e.status, e.owner, e.due_date, e.url
        FROM external_items e
        LEFT JOIN external_imports x ON x.external_item_id = e.id
        WHERE x.external_item_id IS NULL
        ORDER BY e.fetched_at DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No new external items to ingest.")
        return

    created = 0
    for row in rows:
        kind, priority = infer_from_external(row["item_type"], row["title"], row["status"])
        risk = infer_risk(row["title"], row["due_date"])
        risk = max(risk, args.min_risk) if kind == "risk" else risk
        source = f"external:{row['connector']}:{row['id']}"
        text = row["title"]

        inbox_id = insert_inbox_item_dedup(
            conn,
            text=text,
            kind=kind,
            owner=row["owner"],
            due_date=row["due_date"],
            confidence=0.7,
            source=source,
        )
        if inbox_id is None:
            continue
        conn.execute(
            """
            INSERT INTO external_imports (external_item_id, inbox_id)
            VALUES (?, ?)
            """,
            (row["id"], inbox_id),
        )
        append_event(
            conn,
            "ingest_external",
            "inbox_item",
            inbox_id,
            json.dumps(
                {
                    "connector": row["connector"],
                    "item_type": row["item_type"],
                    "priority": priority,
                    "risk": risk,
                    "url": row["url"],
                },
                ensure_ascii=True,
            ),
        )
        created += 1

    conn.commit()
    print(f"Ingested {created} external items into inbox.")
    print("Next: run `myos triage` to convert them into work items.")


def cmd_inbox_process(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, media_type, file_path, COALESCE(transcript_text, extracted_text, '') AS text
        FROM media_assets
        WHERE id NOT IN (SELECT media_asset_id FROM media_imports)
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    created = 0
    for row in rows:
        text = row["text"].strip()
        if not text:
            continue
        suggestions = extract_suggestions(text)
        for s in suggestions:
            if s.confidence < args.min_confidence:
                continue
            inserted = insert_inbox_item_dedup(
                conn,
                text=s.text,
                kind=s.kind,
                owner=None,
                due_date=None,
                confidence=s.confidence,
                source=f"media_asset:{row['id']}",
            )
            if inserted is not None:
                created += 1
        conn.execute(
            "INSERT OR IGNORE INTO media_imports (media_asset_id) VALUES (?)",
            (row["id"],),
        )
    conn.commit()
    print(f"Inbox process complete. Created {created} suggested items.")
