from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import time
import uuid
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime
from pathlib import Path
import subprocess

from .connectors import AhaConnector, ConfluenceConnector, GitHubConnector, JiraConnector
from .dashboard import render_dashboard_html, serve_dashboard
from .db import append_event, get_connection
from .extraction import extract_suggestions
from .graph import connect_work_items
from .ingest.audio import transcribe_audio
from .ingest.image import extract_image_text
from .pulse import detect_mode, run_cycle
from .retrieval import hybrid_score
from . import assistant, autonomy, context as ctx, em, providers, queries, watch
# Helpers extracted out of this module (refactor #12) — re-imported so existing
# call sites (and tests importing them from cli) keep working unchanged.
from .inbox import (
    index_chunk, ensure_work_item_node, infer_kind, infer_priority, infer_risk,
    infer_from_external, insert_inbox_item_dedup,
)
from .locks import acquire_lock, release_lock
from .privacy import (
    get_policy_map, _policy_bool, apply_privacy_filters, redact_obj, _file_sha256, _cleanup_policy_retention,
)
from .planner import (
    _agent_analogies, _agent_plan, _agent_action_specs, _normalize_ai_plan,
    _normalize_ai_actions, _ai_reason_artifacts,
)
from .execution import (
    _PROTECTED_PATCH_PATTERNS, _patch_target_paths, _status_from_result,
    _execute_agent_action, _execute_action_provider, _read_provider_stdin, _outbox_write,
    _provider_body, _provider_target_summary, _post_jira_comment, _post_github_comment,
    _handle_proposals, approve_and_execute,
)
from .autopilot import (
    _create_agent_task, _autopilot_signal_exists, _detect_autopilot_signals,
    _record_signal_and_task, _execute_safe_autopilot_actions, _build_autopilot_digest,
    _store_autopilot_digest, _notify_digest,
)


def load_env_file(path: str) -> int:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return 0
    loaded = 0
    for line in env_path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and value and not os.getenv(key):
            os.environ[key] = value
            loaded += 1
    return loaded


def cmd_capture(args: argparse.Namespace) -> None:
    conn = get_connection()
    # Redact at the boundary: this raw text lands in inbox_items and is later indexed
    # verbatim into the FTS-backed text_chunks at triage time (finding #8). Infer the kind
    # from the redacted text — redaction labels don't disturb keyword inference.
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


def _is_watchable_file(path: Path) -> bool:
    return (
        not path.is_symlink()
        and path.is_file()
        and path.suffix.lower() in {".txt", ".md", ".markdown", ".log"}
    )


def _scan_watch_dirs(conn, *, limit: int = 20, min_confidence: float = 0.65) -> tuple[int, int]:
    policy = get_policy_map(conn)
    max_file_bytes = int(policy.get("watch_max_file_bytes", str(2 * 1024 * 1024)))
    max_candidates = max(limit * 50, 100)
    watch_dirs = conn.execute(
        """
        SELECT id, path
        FROM assistant_watch_dirs
        WHERE status='active'
        ORDER BY id ASC
        """
    ).fetchall()
    files_ingested = 0
    suggestions_created = 0
    candidates_seen = 0
    for watch in watch_dirs:
        root = Path(watch["path"]).expanduser()
        if not root.exists() or not root.is_dir():
            continue
        root_resolved = root.resolve()
        for path in root.rglob("*"):
            candidates_seen += 1
            if candidates_seen > max_candidates:
                return files_ingested, suggestions_created
            if files_ingested >= limit:
                return files_ingested, suggestions_created
            if not _is_watchable_file(path):
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root_resolved):
                    continue
                if path.stat().st_size > max_file_bytes:
                    continue
            except OSError:
                continue
            file_hash = _file_sha256(path)
            reserve = conn.execute(
                """
                INSERT OR IGNORE INTO file_ingests (watch_dir_id, file_path, file_hash, status)
                VALUES (?, ?, ?, 'processing')
                """,
                (watch["id"], str(path), file_hash),
            )
            if reserve.rowcount == 0:
                continue
            raw_text = path.read_text(errors="replace")
            filtered = apply_privacy_filters(conn, raw_text)
            if not filtered.strip():
                conn.execute(
                    "UPDATE file_ingests SET status='skipped_empty' WHERE file_path=? AND file_hash=?",
                    (str(path), file_hash),
                )
                continue
            conn.execute(
                """
                INSERT INTO media_assets (media_type, file_path, transcript_text, source)
                VALUES ('file', ?, ?, 'watch_dir')
                """,
                (str(path), filtered),
            )
            media_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                "INSERT OR IGNORE INTO media_imports (media_asset_id) VALUES (?)",
                (media_id,),
            )
            conn.execute(
                """
                INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet)
                VALUES ('file', ?, 'watch_dir', '1', 0.75, ?)
                """,
                (str(path), filtered[:400]),
            )
            provenance_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            index_chunk(conn, "media_asset", media_id, filtered, provenance_id=provenance_id)
            for suggestion in extract_suggestions(filtered):
                if suggestion.confidence < min_confidence:
                    continue
                inserted = insert_inbox_item_dedup(
                    conn,
                    text=suggestion.text,
                    kind=suggestion.kind,
                    owner=None,
                    due_date=None,
                    confidence=suggestion.confidence,
                    source=f"watch_file:{file_hash}",
                )
                if inserted is not None:
                    suggestions_created += 1
            conn.execute(
                """
                UPDATE file_ingests
                SET status='ingested', media_asset_id=?
                WHERE file_path=? AND file_hash=?
                """,
                (media_id, str(path), file_hash),
            )
            files_ingested += 1
    return files_ingested, suggestions_created


def cmd_triage(_: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM inbox_items WHERE status = 'new' ORDER BY created_at ASC"
    ).fetchall()

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


def cmd_close_day(args: argparse.Namespace) -> None:
    conn = get_connection()
    open_count = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status = 'open'").fetchone()["c"]
    high_risk = conn.execute(
        "SELECT COUNT(*) AS c FROM work_items WHERE status = 'open' AND risk_score >= 60"
    ).fetchone()["c"]

    mode = args.mode
    summary = (
        f"Closed day with {open_count} open items and {high_risk} high-risk items at "
        f"{datetime.now().isoformat(timespec='minutes')}"
    )

    conn.execute(
        "INSERT INTO daily_logs (summary, mode, note) VALUES (?, ?, ?)",
        (summary, mode, args.note),
    )
    append_event(
        conn,
        "close_day",
        "daily_log",
        None,
        json.dumps({"mode": mode, "open_items": open_count, "high_risk": high_risk}, ensure_ascii=True),
    )
    conn.commit()

    print("Day closed.")
    print(summary)
    if args.note:
        print(f"Note: {args.note}")


def cmd_transcribe(args: argparse.Namespace) -> None:
    audio_path = args.audio_file
    transcript = transcribe_audio(audio_path, args.text)
    if not transcript:
        print("No transcript produced. Install 'faster-whisper' or provide --text.")
        return

    conn = get_connection()
    filtered = apply_privacy_filters(conn, transcript)
    conn.execute(
        """
        INSERT INTO media_assets (media_type, file_path, transcript_text, source)
        VALUES (?, ?, ?, ?)
        """,
        ("audio", audio_path, filtered, "local"),
    )
    media_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        """
        INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("audio", audio_path, "whisper_or_manual", "1", 0.7 if args.text else 0.82, filtered[:400]),
    )
    provenance_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    index_chunk(conn, "media_asset", media_id, filtered, provenance_id=provenance_id)
    append_event(
        conn,
        "ingest_audio",
        "media_asset",
        media_id,
        json.dumps({"path": audio_path}, ensure_ascii=True),
    )
    conn.commit()
    print(f"Transcript stored as media asset #{media_id}.")
    print("Run: myos inbox-process to generate suggested tasks.")


def cmd_ingest_image(args: argparse.Namespace) -> None:
    image_path = args.image_file
    extracted = extract_image_text(image_path, args.text)
    if not extracted:
        print("Could not extract OCR text. Install tesseract or pass --text manually.")
        return

    conn = get_connection()
    filtered = apply_privacy_filters(conn, extracted)
    conn.execute(
        """
        INSERT INTO media_assets (media_type, file_path, extracted_text, source)
        VALUES (?, ?, ?, ?)
        """,
        ("image", image_path, filtered, "local"),
    )
    media_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        """
        INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("image", image_path, "ocr_or_manual", "1", 0.68 if args.text else 0.8, filtered[:400]),
    )
    provenance_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    index_chunk(conn, "media_asset", media_id, filtered, provenance_id=provenance_id)
    append_event(
        conn,
        "ingest_image",
        "media_asset",
        media_id,
        json.dumps({"path": image_path}, ensure_ascii=True),
    )
    conn.commit()

    print(f"Image text stored as media asset #{media_id}.")
    print("Tip: run `myos context \"<topic>\"` to retrieve relevant chunks.")


def cmd_link(args: argparse.Namespace) -> None:
    conn = get_connection()
    connect_work_items(conn, args.from_item, args.to_item, args.relation, args.weight)
    append_event(
        conn,
        "link",
        "knowledge_edge",
        None,
        json.dumps(
            {"from_item": args.from_item, "to_item": args.to_item, "relation": args.relation},
            ensure_ascii=True,
        ),
    )
    conn.commit()
    print(
        f"Linked work item {args.from_item} -> {args.to_item} "
        f"with relation '{args.relation}'."
    )


def cmd_related(args: argparse.Namespace) -> None:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM knowledge_nodes WHERE node_type = 'work_item' AND ref_id = ?",
        (args.item,),
    ).fetchone()
    node_id = int(row["id"]) if row else None
    if node_id is None:
        print("Work item is not indexed. Run `myos triage` first.")
        return

    rows = conn.execute(
        """
        SELECT
            e.relation AS relation,
            e.weight AS weight,
            w.id AS work_item_id,
            w.title AS title,
            w.status AS status
        FROM knowledge_edges e
        JOIN knowledge_nodes n ON n.id = e.to_node_id
        JOIN work_items w ON w.id = n.ref_id
        WHERE e.from_node_id = ? AND n.node_type = 'work_item'
        ORDER BY e.weight DESC, w.id ASC
        LIMIT ?
        """,
        (node_id, args.limit),
    ).fetchall()

    if not rows:
        print("No related work items found.")
        return

    print(f"Related work for item {args.item}:")
    for r in rows:
        print(
            f"- #{r['work_item_id']} [{r['relation']}] {r['title']} "
            f"(status={r['status']}, weight={r['weight']:.2f})"
        )


def cmd_context(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT source_type, source_id, content
        FROM text_chunks
        ORDER BY created_at DESC
        LIMIT 400
        """
    ).fetchall()
    if not rows:
        print("No context chunks indexed yet.")
        return

    scored = []
    for row in rows:
        score = hybrid_score(args.query, row["content"])
        if score > 0:
            scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: args.limit]
    if not top:
        print("No relevant context found.")
        return

    print(f"Context results for: {args.query}")
    for score, row in top:
        snippet = row["content"].strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        print(
            f"- ({score:.3f}) {row['source_type']}#{row['source_id']}: {snippet}"
        )


def cmd_recall(args: argparse.Namespace) -> None:
    """Scored recall over the conversation memory: relevance + recency + importance."""
    conn = get_connection()
    hits = ctx.scored_retrieve(conn, args.query, limit=args.limit)
    if hits:
        print(f"Recall for: {args.query}  (score = relevance + recency + importance)")
        for h in hits:
            subj = f" [{h['subject']}]" if h.get("subject") else ""
            print(f"- ({h['score']}) {h['kind']}{subj}: {h['detail']}")
            print(f"    rel={h['relevance']} rec={h['recency']} imp={h['importance']}")
        return
    # No scored observation hit yet — fall back to raw indexed-chunk recall.
    chunks = queries.context_search(conn, args.query, limit=args.limit)
    if not chunks:
        print("No relevant context found.")
        return
    print(f"Context (chunks) for: {args.query}")
    for c in chunks:
        snip = c["snippet"].replace("\n", " ")
        print(f"- ({c['score']}) {c['source_type']}#{c['source_id']}: {snip}")


def cmd_reflect(_: argparse.Namespace) -> None:
    """Distill recent observations into insights + relationship edges, then run hygiene."""
    conn = get_connection()
    r = ctx.reflect(conn)
    h = ctx.hygiene(conn)
    print(f"Reflection: {r['insights']} insight(s) across {r['subjects']} subject(s); "
          f"{r.get('suggestions', 0)} new suggestion(s).")
    print(f"Hygiene: merged {h['merged']} duplicate(s), decayed {h['decayed']} stale observation(s).")
    if r.get("suggestions"):
        print("Review them: myos suggestions list")
    rels = ctx.relationships(conn, limit=8)
    if rels:
        print("Top relationships:")
        for rel in rels:
            print(f"- {rel['a']} ↔ {rel['b']} (weight {rel['weight']:.0f})")
    insights = conn.execute(
        "SELECT summary FROM context_insights WHERE superseded_by IS NULL ORDER BY created_at DESC LIMIT 8"
    ).fetchall()
    if insights:
        print("Recent insights:")
        for ins in insights:
            print(f"- {ins['summary']}")


def cmd_suggestions(args: argparse.Namespace) -> None:
    """List / accept / dismiss / apply tracked improvement suggestions (gated — nothing
    executes from here; accepting only records the decision)."""
    conn = get_connection()
    action = getattr(args, "suggestions_action", "list") or "list"
    if action in ("accept", "dismiss", "apply"):
        if args.id is None:
            print(f"Usage: myos suggestions {action} <id>")
            raise SystemExit(1)
        decision = {"accept": "accepted", "dismiss": "dismissed", "apply": "applied"}[action]
        res = ctx.decide_suggestion(conn, args.id, decision, feedback=getattr(args, "feedback", "") or "")
        if res.get("error"):
            print(res["error"])
            raise SystemExit(1)
        print(f"Suggestion #{res['id']} → {res['status']}.")
        return
    rows = ctx.list_suggestions(conn, status=getattr(args, "status", "proposed") or "proposed")
    if not rows:
        print("No open suggestions.")
        return
    print("Improvement suggestions (propose-and-approve; nothing auto-applies):")
    for r in rows:
        print(f"#{r['id']} [{r['status']}] {r['title']}")
        if r.get("rationale"):
            print(f"    why: {r['rationale']}")
    print("Accept: myos suggestions accept <id>   Dismiss: myos suggestions dismiss <id>")


def cmd_memory(_: argparse.Namespace) -> None:
    """One-glance view of what the Context Intelligence Loop has learned."""
    conn = get_connection()
    s = ctx.summary(conn)
    print("MYOS memory & context intelligence")
    print(f"- conversations: {s['conversations']}  turns logged: {s['turns']}")
    print(f"- active observations: {s['observations_active']}  insights: {s['insights']}")
    print(f"- open suggestions: {s['suggestions_open']}  derived relationships: {s['relationships']}")
    rels = ctx.relationships(conn, limit=5)
    if rels:
        print("Strongest relationships:")
        for rel in rels:
            print(f"- {rel['a']} ↔ {rel['b']} (weight {rel['weight']:.0f})")


def cmd_reindex(_: argparse.Namespace) -> None:
    conn = get_connection()
    items = conn.execute(
        "SELECT id, title FROM work_items ORDER BY id ASC"
    ).fetchall()

    chunks_added = 0
    nodes_added = 0
    for item in items:
        before = conn.execute(
            "SELECT id FROM knowledge_nodes WHERE node_type = 'work_item' AND ref_id = ?",
            (item["id"],),
        ).fetchone()
        ensure_work_item_node(conn, int(item["id"]), item["title"])
        if not before:
            nodes_added += 1

        has_chunk = conn.execute(
            "SELECT id FROM text_chunks WHERE source_type = 'work_item' AND source_id = ? LIMIT 1",
            (item["id"],),
        ).fetchone()
        if not has_chunk:
            # Only increment if a chunk was actually written (index_chunk skips
            # whitespace-only titles; counting them causes a never-ending re-attempt
            # on every future reindex) (review R4-6).
            if index_chunk(conn, "work_item", int(item["id"]), item["title"]):
                chunks_added += 1

    conn.commit()
    print(
        f"Reindex complete. Added {nodes_added} nodes and {chunks_added} chunks for existing work items."
    )


def cmd_sync(args: argparse.Namespace) -> None:
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


def cmd_doctor(_: argparse.Namespace) -> None:
    conn = get_connection()
    print("System health:")
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM inbox_items) AS inbox_count,
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_work,
          (SELECT COUNT(*) FROM external_items) AS external_count,
          (SELECT COUNT(*) FROM event_log) AS event_count
        """
    ).fetchone()
    print(
        f"- inbox={counts['inbox_count']} open_work={counts['open_work']} "
        f"external={counts['external_count']} events={counts['event_count']}"
    )

    print(f"Autonomy level: {autonomy.level_from_policy(conn)} (auto-run safe / one-tap non-destructive / block destructive)")
    active = providers.resolve_backend_name()
    print(f"Agent backends (active: {active}):")
    for b in providers.available_backends():
        mark = "✅" if b["available"] else "❌"
        print(f"- {mark} {b['name']}: {b['detail']}")

    rows = conn.execute(
        """
        SELECT connector, last_status, last_success_at, last_error
        FROM sync_state
        ORDER BY connector ASC
        """
    ).fetchall()
    if not rows:
        print("- sync_state: no connector runs yet")
        return
    print("Connector status:")
    for row in rows:
        err = f" err={row['last_error']}" if row["last_error"] else ""
        print(
            f"- {row['connector']}: status={row['last_status']} "
            f"last_success={row['last_success_at']}{err}"
        )


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


def cmd_why(args: argparse.Namespace) -> None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT w.id, w.title, w.kind, w.risk_score, p.source_type, p.source_ref, p.extractor, p.confidence, p.snippet
        FROM work_items w
        LEFT JOIN text_chunks tc ON tc.source_type='work_item' AND tc.source_id=w.id
        LEFT JOIN provenance p ON p.id=tc.provenance_id
        WHERE w.id = ?
        LIMIT 1
        """,
        (args.item,),
    ).fetchone()
    if not row:
        print("Work item not found.")
        return
    print(f"Work item #{row['id']}: {row['title']}")
    print(f"kind={row['kind']} risk={row['risk_score']}")
    if row["extractor"]:
        print(
            f"provenance: extractor={row['extractor']} source={row['source_type']}:{row['source_ref']} confidence={row['confidence']}"
        )
    if row["snippet"]:
        print(f"snippet: {row['snippet'][:180]}")


def cmd_at_risk(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, title, risk_score, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT ?
        """,
        (args.threshold, args.limit),
    ).fetchall()
    if not rows:
        print("No at-risk items.")
        return
    print("At-risk items:")
    for row in rows:
        print(f"- #{row['id']} {row['title']} | risk={row['risk_score']} | due={row['due_date'] or 'none'}")


def cmd_waiting_on(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, title, owner, due_date
        FROM work_items
        WHERE status='open' AND kind IN ('risk', 'commitment') AND owner IS NOT NULL
        ORDER BY COALESCE(due_date, '9999-12-31') ASC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No waiting-on style items found.")
        return
    for row in rows:
        print(f"- #{row['id']} waiting on {row['owner']}: {row['title']} (due={row['due_date'] or 'none'})")


def cmd_delegation_candidates(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, title, kind, risk_score
        FROM work_items
        WHERE status='open' AND kind IN ('task', 'commitment')
        ORDER BY priority ASC, risk_score DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No delegation candidates.")
        return
    print("Delegation candidates:")
    for row in rows:
        print(f"- #{row['id']} [{row['kind']}] {row['title']} (risk={row['risk_score']})")


def cmd_brief(args: argparse.Namespace) -> None:
    conn = get_connection()
    mode = detect_mode(args.meeting_hours)
    open_items = conn.execute(
        """
        SELECT id, title, kind, risk_score, due_date
        FROM work_items
        WHERE status='open'
        ORDER BY priority ASC, risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT ?
        """,
        (args.top,),
    ).fetchall()
    at_risk = conn.execute(
        """
        SELECT id, title, risk_score, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 5
        """,
        (args.risk_threshold,),
    ).fetchall()
    waiting = conn.execute(
        """
        SELECT id, title, owner, due_date
        FROM work_items
        WHERE status='open' AND owner IS NOT NULL AND kind IN ('commitment', 'risk')
        ORDER BY COALESCE(due_date, '9999-12-31') ASC
        LIMIT 5
        """
    ).fetchall()

    print(f"Executive brief | mode={mode} | meeting_hours={args.meeting_hours}")
    print("\nTop outcomes:")
    for idx, row in enumerate(open_items[:3], start=1):
        print(
            f"{idx}. #{row['id']} {row['title']} "
            f"(kind={row['kind']}, risk={row['risk_score']}, due={row['due_date'] or 'none'})"
        )
    if not open_items:
        print("- No open items.")

    print("\nAt-risk:")
    if not at_risk:
        print("- None")
    else:
        for row in at_risk:
            print(f"- #{row['id']} {row['title']} (risk={row['risk_score']}, due={row['due_date'] or 'none'})")

    print("\nWaiting-on:")
    if not waiting:
        print("- None")
    else:
        for row in waiting:
            print(f"- #{row['id']} waiting on {row['owner']}: {row['title']} (due={row['due_date'] or 'none'})")

    if mode == "meeting-heavy":
        print("\nGuidance:")
        print("- Convert deep work into 1-2 tiny wins.")
        print("- Prioritize commitments, decisions, and delegation.")
        print("- Use `myos stop-doing` before accepting new work.")


def cmd_stop_doing(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, title, kind, risk_score, due_date
        FROM work_items
        WHERE status='open'
        ORDER BY priority ASC, risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 30
        """
    ).fetchall()
    if not rows:
        print("No open items.")
        return

    suggestions: list[tuple[str, int, str]] = []
    capacity = args.capacity
    deep_budget = args.deep_budget
    deep_candidates = 0
    for row in rows:
        title = row["title"].lower()
        is_deep = any(k in title for k in ["implement", "refactor", "design", "migrate", "build"])
        if is_deep:
            deep_candidates += 1
        if row["risk_score"] < args.keep_risk and row["kind"] in ("task", "note"):
            suggestions.append(("defer", row["id"], row["title"]))
        elif row["kind"] == "task" and row["risk_score"] < 55:
            suggestions.append(("delegate", row["id"], row["title"]))

    print(
        f"Stop-doing review | open={len(rows)} capacity={capacity} deep_budget={deep_budget} "
        f"deep_candidates={deep_candidates}"
    )
    if len(rows) > capacity:
        print(f"- Over capacity by {len(rows) - capacity} items. Defer or delegate lowest-impact work.")
    if deep_candidates > deep_budget:
        print(f"- Deep-work overload: {deep_candidates} deep items > budget {deep_budget}.")

    if not suggestions:
        print("- No strong defer/delegate candidates based on current thresholds.")
        return

    print("\nSuggested actions:")
    for action, item_id, title in suggestions[: args.limit]:
        print(f"- {action.upper()}: #{item_id} {title}")
    append_event(
        conn,
        "stop_doing_review",
        "work_item",
        None,
        json.dumps({"suggestions": len(suggestions), "capacity": capacity}, ensure_ascii=True),
    )
    conn.commit()


def cmd_onboard(_: argparse.Namespace) -> None:
    mapping = {
        "jira": (JiraConnector, ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"]),
        "github": (GitHubConnector, ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"]),
        "confluence": (
            ConfluenceConnector,
            ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        ),
        "aha": (AhaConnector, ["AHA_BASE_URL", "AHA_API_TOKEN"]),
    }
    print("Onboarding diagnostics:")
    ready = 0
    for name, (_, keys) in mapping.items():
        missing = [k for k in keys if not os.getenv(k)]
        if missing:
            print(f"- {name}: MISSING {', '.join(missing)}")
        else:
            print(f"- {name}: READY")
            ready += 1
    print(f"\nConnectors ready: {ready}/{len(mapping)}")
    if ready < len(mapping):
        print("Set missing environment variables, then run: myos sync --connector all")
    else:
        print("All connectors ready. Run: myos run-day --meeting-hours <n>")


def cmd_config_init(args: argparse.Namespace) -> None:
    target = Path(args.path).expanduser()
    if target.exists() and not args.force:
        print(f"Config already exists: {target}")
        print("Use --force to overwrite.")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "# Personal Assistant OS credentials",
                "JIRA_BASE_URL=",
                "JIRA_USER_EMAIL=",
                "JIRA_API_TOKEN=",
                "",
                "GITHUB_TOKEN=",
                "GITHUB_OWNER=",
                "GITHUB_REPO=",
                "",
                "CONFLUENCE_BASE_URL=",
                "CONFLUENCE_USER_EMAIL=",
                "CONFLUENCE_API_TOKEN=",
                "",
                "AHA_BASE_URL=",
                "AHA_API_TOKEN=",
                "",
            ]
        )
        + "\n"
    )
    print(f"Created config template: {target}")
    print("Fill values, then run: myos run-day --env-file " + str(target))


def _env_template(db_path: Path) -> str:
    return "\n".join(
        [
            "# Personal Assistant OS live configuration",
            f"MYOS_DB_PATH={db_path}",
            "",
            "# Jira",
            "JIRA_BASE_URL=",
            "JIRA_USER_EMAIL=",
            "JIRA_API_TOKEN=",
            "",
            "# GitHub",
            "GITHUB_TOKEN=",
            "GITHUB_OWNER=",
            "GITHUB_REPO=",
            "",
            "# Confluence",
            "CONFLUENCE_BASE_URL=",
            "CONFLUENCE_USER_EMAIL=",
            "CONFLUENCE_API_TOKEN=",
            "",
            "# Aha",
            "AHA_BASE_URL=",
            "AHA_API_TOKEN=",
            "",
            "# Optional AI reasoning provider",
            "MYOS_AI_PROVIDER=local",
            "MYOS_AI_COMMAND=",
            "",
            "# Safe default: approved external actions go to local outbox",
            "MYOS_ACTION_PROVIDER=builtin",
            "MYOS_ACTION_COMMAND=myos action-provider",
            "",
            "# Optional notification hook for assistant digests",
            "MYOS_NOTIFY_COMMAND=",
            "",
        ]
    ) + "\n"


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _setup_live_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    project_root = Path(__file__).resolve().parents[2]
    data_dir = (Path(args.data_dir).expanduser() if args.data_dir else project_root / "data").resolve()
    env_path = (Path(args.env_file).expanduser() if args.env_file else data_dir / ".env.myos").resolve()
    env_values = _read_env_values(env_path)
    configured_db = args.db_path or os.getenv("MYOS_DB_PATH", "") or env_values.get("MYOS_DB_PATH", "")
    db_path = (Path(configured_db).expanduser() if configured_db else data_dir / "assistant.db").resolve()
    watch_dir = (Path(args.watch_dir).expanduser() if args.watch_dir else data_dir / "inbox").resolve()
    return data_dir, env_path, db_path, watch_dir


def _env_or_file(key: str, values: dict[str, str]) -> str:
    return os.getenv(key, "") or values.get(key, "")


def _cmd_setup_live_check(env_path: Path, db_path: Path, watch_dir: Path) -> bool:
    env_values = _read_env_values(env_path)
    print("Live readiness check:")
    ok_count = 0
    total = 0

    def check(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        nonlocal ok_count, total
        if not required:
            print(f"- {'PASS' if ok else 'INFO'} {name}: {detail}")
            return
        total += 1
        if ok:
            ok_count += 1
        print(f"- {'PASS' if ok else 'WARN'} {name}: {detail}")

    check("env_file", env_path.exists(), str(env_path) if env_path.exists() else f"missing {env_path}")
    if env_path.exists():
        mode = env_path.stat().st_mode & 0o777
        check("env_permissions", mode & 0o077 == 0, oct(mode))
    else:
        check("env_permissions", False, "env file missing")

    credential_groups = {
        "jira_credentials": ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"],
        "github_credentials": ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"],
        "confluence_credentials": ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        "aha_credentials": ["AHA_BASE_URL", "AHA_API_TOKEN"],
    }
    for name, keys in credential_groups.items():
        missing = [key for key in keys if not _env_or_file(key, env_values)]
        check(name, not missing, "ready" if not missing else "missing " + ", ".join(missing))

    action_provider = _env_or_file("MYOS_ACTION_COMMAND", env_values)
    check("action_provider", bool(action_provider), action_provider or "missing MYOS_ACTION_COMMAND")
    check("watch_dir", watch_dir.exists(), str(watch_dir) if watch_dir.exists() else f"missing {watch_dir}")
    check("database_file", db_path.exists(), str(db_path) if db_path.exists() else f"missing {db_path}")

    if not db_path.exists():
        print(f"Readiness summary: {ok_count}/{total} checks passing")
        print("Next: run `myos setup-live --apply`, then fill the env file.")
        return ok_count == total

    try:
        conn = sqlite3.connect(db_path)
        active_goals = conn.execute("SELECT COUNT(*) FROM assistant_goals WHERE status='active'").fetchone()[0]
        active_watch_dirs = conn.execute("SELECT COUNT(*) FROM assistant_watch_dirs WHERE status='active'").fetchone()[0]
        recent_autopilot = conn.execute(
            "SELECT COUNT(*) FROM autopilot_runs WHERE started_at >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        conn.close()
        check("standing_goals", active_goals > 0, f"active_goals={active_goals}")
        check("watch_config", active_watch_dirs > 0, f"active_watch_dirs={active_watch_dirs}")
        check("autopilot_smoke", recent_autopilot > 0, f"runs_24h={recent_autopilot}", required=False)
    except sqlite3.Error as exc:
        check("database_schema", False, str(exc))

    print(f"Readiness summary: {ok_count}/{total} checks passing")
    if ok_count == total:
        print(f"Ready: myos autopilot --env-file {env_path} --once")
    else:
        print(f"Next: fix WARN items, then run `myos autopilot --env-file {env_path} --once`.")
    return ok_count == total


def cmd_setup_live(args: argparse.Namespace) -> None:
    data_dir, env_path, db_path, watch_dir = _setup_live_paths(args)
    goals = [
        (
            "Keep my work commitments and risks current",
            "Monitor synced work, notes, transcripts, due dates, blockers, and approval-needed updates.",
            240,
            1,
        ),
        (
            "Prepare daily executive digest",
            "Summarize what changed, what was handled, what needs approval, and the next best action.",
            720,
            2,
        ),
    ]

    if args.check:
        if not _cmd_setup_live_check(env_path, db_path, watch_dir):
            raise SystemExit(1)
        return

    print("Live setup plan:")
    print(f"- data_dir: {data_dir}")
    print(f"- env_file: {env_path}")
    print(f"- db_path: {db_path}")
    print(f"- default_watch_dir: {watch_dir}")
    print("- default action provider: MYOS_ACTION_COMMAND=myos action-provider")
    print("- default goals: commitment/risk monitoring, daily digest")
    print("- launchd autopilot: " + ("yes" if args.install_launchd else "no"))
    if not args.apply:
        print("Dry run only. Re-run with --apply to create files and DB records.")
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "autopilot").mkdir(parents=True, exist_ok=True)
    (data_dir / "outbox").mkdir(parents=True, exist_ok=True)
    watch_dir.mkdir(parents=True, exist_ok=True)
    if not env_path.exists() or args.force:
        env_path.write_text(_env_template(db_path))
        env_path.chmod(0o600)
        print(f"Wrote env template: {env_path}")
    else:
        env_path.chmod(0o600)
        print(f"Env file already exists: {env_path}")

    os.environ["MYOS_DB_PATH"] = str(db_path)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO assistant_watch_dirs (path, label, status, updated_at)
        VALUES (?, 'default-inbox', 'active', CURRENT_TIMESTAMP)
        ON CONFLICT(path) DO UPDATE SET status='active', updated_at=CURRENT_TIMESTAMP
        """,
        (str(watch_dir),),
    )
    for objective, context, cadence, priority in goals:
        existing = conn.execute(
            "SELECT id FROM assistant_goals WHERE objective=? LIMIT 1",
            (objective,),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO assistant_goals (objective, context, cadence_minutes, priority, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (objective, context, cadence, priority),
        )
    conn.execute(
        """
        INSERT INTO assistant_policies (key, value, updated_at)
        VALUES ('action_timeout_sec', '30', CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """
    )
    conn.commit()
    print("Configured default watch directory, goals, and policy.")

    if args.install_launchd:
        cmd_launchd_install(
            argparse.Namespace(
                apply=True,
                load=args.load_launchd,
                env_file=str(env_path),
                interval_sec=1800,
                meeting_hours=0.0,
                autopilot=True,
                autopilot_interval_sec=args.autopilot_interval_sec,
            )
        )

    print("Setup complete.")
    print(f"Next: fill credentials in {env_path}")
    print(f"Then: myos autopilot --env-file {env_path} --once")
    print("Review: myos digest && myos approve --list && myos self-review")


def cmd_report(args: argparse.Namespace) -> None:
    conn = get_connection()
    mode = detect_mode(args.meeting_hours)
    top_rows = conn.execute(
        """
        SELECT id, title, kind, risk_score, due_date
        FROM work_items
        WHERE status='open'
        ORDER BY priority ASC, risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 5
        """
    ).fetchall()
    risk_rows = conn.execute(
        """
        SELECT id, title, risk_score, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC
        LIMIT 5
        """,
        (args.risk_threshold,),
    ).fetchall()
    sync_rows = conn.execute(
        """
        SELECT connector, last_status, last_success_at, last_error
        FROM sync_state
        ORDER BY connector ASC
        """
    ).fetchall()

    report_dir = Path(args.output_dir) if args.output_dir else Path(__file__).resolve().parents[2] / "data" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = report_dir / f"daily-brief-{ts}.md"

    lines = [
        f"# Daily Brief ({datetime.now().isoformat(timespec='minutes')})",
        "",
        f"- Mode: `{mode}`",
        f"- Meeting hours: `{args.meeting_hours}`",
        "",
        "## Top Outcomes",
    ]
    if top_rows:
        for row in top_rows[:3]:
            lines.append(
                f"- #{row['id']} {row['title']} (kind={row['kind']}, risk={row['risk_score']}, due={row['due_date'] or 'none'})"
            )
    else:
        lines.append("- No open work items.")

    lines.extend(["", "## At-Risk"])
    if risk_rows:
        for row in risk_rows:
            lines.append(f"- #{row['id']} {row['title']} (risk={row['risk_score']}, due={row['due_date'] or 'none'})")
    else:
        lines.append("- None")

    lines.extend(["", "## Connector Health"])
    if sync_rows:
        for row in sync_rows:
            suffix = f" error={row['last_error']}" if row["last_error"] else ""
            lines.append(
                f"- {row['connector']}: status={row['last_status']} last_success={row['last_success_at']}{suffix}"
            )
    else:
        lines.append("- No connector sync state found.")

    report_path.write_text("\n".join(lines) + "\n")
    print(f"Report generated: {report_path}")


def cmd_run_day(args: argparse.Namespace) -> None:
    if args.env_file:
        loaded = load_env_file(args.env_file)
        print(f"Loaded {loaded} vars from {args.env_file}")
    print("Running day pipeline: sync -> ingest-external -> inbox-process -> triage -> brief -> stop-doing -> report")
    conn = get_connection()
    lock_owner = f"run-day:{uuid.uuid4()}"
    if not acquire_lock(conn, "run_day", lock_owner):
        print("Another run-day pipeline is active. Skipping this run.")
        return {"status": "skipped", "details": "run_day lock already held"}
    connectors = {
        "jira": JiraConnector,
        "github": GitHubConnector,
        "confluence": ConfluenceConnector,
        "aha": AhaConnector,
    }
    sync_targets = [args.connector] if args.connector != "all" else list(connectors.keys())
    try:
        for name in sync_targets:
            result = connectors[name](conn).sync()
            print(f"SYNC {result.connector}: status={result.status} fetched={result.fetched} msg={result.message}")

        ext_rows = conn.execute(
            """
            SELECT e.id, e.connector, e.item_type, e.title, e.status, e.owner, e.due_date, e.url
            FROM external_items e
            LEFT JOIN external_imports x ON x.external_item_id = e.id
            WHERE x.external_item_id IS NULL
            ORDER BY e.fetched_at DESC
            LIMIT ?
            """,
            (args.external_limit,),
        ).fetchall()
        external_created = 0
        for row in ext_rows:
            kind, _ = infer_from_external(row["item_type"], row["title"], row["status"])
            inbox_id = insert_inbox_item_dedup(
                conn,
                text=row["title"],
                kind=kind,
                owner=row["owner"],
                due_date=row["due_date"],
                confidence=0.7,
                source=f"external:{row['connector']}:{row['id']}",
            )
            if inbox_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO external_imports (external_item_id, inbox_id) VALUES (?, ?)",
                (row["id"], inbox_id),
            )
            external_created += 1

        media_rows = conn.execute(
            """
            SELECT id, COALESCE(transcript_text, extracted_text, '') AS text
            FROM media_assets
            WHERE id NOT IN (SELECT media_asset_id FROM media_imports)
            ORDER BY id DESC
            LIMIT ?
            """,
            (args.media_limit,),
        ).fetchall()
        media_created = 0
        for row in media_rows:
            text = row["text"].strip()
            if not text:
                continue
            for s in extract_suggestions(text):
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
                    media_created += 1
            conn.execute(
                "INSERT OR IGNORE INTO media_imports (media_asset_id) VALUES (?)",
                (row["id"],),
            )

        triage_rows = conn.execute("SELECT * FROM inbox_items WHERE status='new' ORDER BY created_at ASC").fetchall()
        triaged = 0
        for row in triage_rows:
            text = row["text"].strip()
            kind = row["kind"] if row["kind"] != "note" else infer_kind(text)
            priority = infer_priority(text, row["due_date"])
            risk_score = infer_risk(text, row["due_date"])
            title = text if len(text) <= 90 else text[:87] + "..."
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO work_items (inbox_id, title, kind, priority, risk_score, owner, due_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], title, kind, priority, risk_score, row["owner"], row["due_date"]),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "UPDATE inbox_items SET status='triaged', triaged_at=CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                continue
            item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            ensure_work_item_node(conn, item_id, title)
            index_chunk(conn, "work_item", item_id, text)
            conn.execute(
                "UPDATE inbox_items SET status='triaged', triaged_at=CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
            triaged += 1

        conn.commit()
        print(f"Pipeline summary: external_ingested={external_created}, media_suggested={media_created}, triaged={triaged}")

        cmd_brief(argparse.Namespace(meeting_hours=args.meeting_hours, top=10, risk_threshold=args.risk_threshold))
        print()
        cmd_stop_doing(
            argparse.Namespace(
                capacity=args.capacity,
                deep_budget=args.deep_budget,
                keep_risk=args.keep_risk,
                limit=args.stop_limit,
            )
        )
        print()
        cmd_report(
            argparse.Namespace(
                meeting_hours=args.meeting_hours,
                risk_threshold=args.risk_threshold,
                output_dir=args.output_dir,
            )
        )
        return {"status": "completed", "details": "run_day pipeline completed"}
    finally:
        release_lock(conn, "run_day", lock_owner)
        conn.commit()


def cmd_go_live(args: argparse.Namespace) -> None:
    if args.env_file:
        loaded = load_env_file(args.env_file)
        print(f"Loaded {loaded} vars from {args.env_file}")

    conn = get_connection()
    lock_owner = f"go-live:{uuid.uuid4()}"
    if not acquire_lock(conn, "go_live", lock_owner):
        print("Another go-live pipeline is active. Skipping this run.")
        return
    connectors = {
        "jira": JiraConnector,
        "github": GitHubConnector,
        "confluence": ConfluenceConnector,
        "aha": AhaConnector,
    }
    targets = [args.connector] if args.connector != "all" else list(connectors.keys())
    results: list[tuple[str, str, int, str]] = []
    try:
        for name in targets:
            res = connectors[name](conn).sync()
            results.append((res.connector, res.status, res.fetched, res.message))
            print(f"GO-LIVE SYNC {res.connector}: status={res.status} fetched={res.fetched} msg={res.message}")

        ext_rows = conn.execute(
            """
            SELECT e.id, e.connector, e.item_type, e.title, e.status, e.owner, e.due_date
            FROM external_items e
            LEFT JOIN external_imports x ON x.external_item_id = e.id
            WHERE x.external_item_id IS NULL
            ORDER BY e.fetched_at DESC
            LIMIT ?
            """,
            (args.external_limit,),
        ).fetchall()
        ingested = 0
        for row in ext_rows:
            kind, _ = infer_from_external(row["item_type"], row["title"], row["status"])
            inbox_id = insert_inbox_item_dedup(
                conn,
                text=row["title"],
                kind=kind,
                owner=row["owner"],
                due_date=row["due_date"],
                confidence=0.7,
                source=f"external:{row['connector']}:{row['id']}",
            )
            if inbox_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO external_imports (external_item_id, inbox_id) VALUES (?, ?)",
                (row["id"], inbox_id),
            )
            ingested += 1

        triage_rows = conn.execute("SELECT * FROM inbox_items WHERE status='new' ORDER BY created_at ASC").fetchall()
        triaged = 0
        for row in triage_rows:
            text = row["text"].strip()
            kind = row["kind"] if row["kind"] != "note" else infer_kind(text)
            priority = infer_priority(text, row["due_date"])
            risk_score = infer_risk(text, row["due_date"])
            title = text if len(text) <= 90 else text[:87] + "..."
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO work_items (inbox_id, title, kind, priority, risk_score, owner, due_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], title, kind, priority, risk_score, row["owner"], row["due_date"]),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "UPDATE inbox_items SET status='triaged', triaged_at=CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                continue
            item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            ensure_work_item_node(conn, item_id, title)
            index_chunk(conn, "work_item", item_id, text)
            if kind == "commitment":
                conn.execute(
                    """
                    INSERT INTO commitment_log (work_item_id, promised_on, due_on, outcome)
                    VALUES (?, CURRENT_TIMESTAMP, ?, 'open')
                    """,
                    (item_id, row["due_date"]),
                )
            conn.execute(
                "UPDATE inbox_items SET status='triaged', triaged_at=CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
            triaged += 1
        conn.commit()

        ok = sum(1 for _, status, _, _ in results if status == "ok")
        skipped = sum(1 for _, status, _, _ in results if status == "skipped")
        err = sum(1 for _, status, _, _ in results if status == "error")
        print("\nGo-live summary:")
        print(f"- connectors_ok={ok} skipped={skipped} error={err}")
        print(f"- external_ingested={ingested} triaged={triaged}")
        if ok == 0:
            print("- Status: not live yet (credentials likely missing). Run `myos onboard` and update env file.")
        elif err > 0:
            print("- Status: partially live. Fix failing connector credentials/endpoints and rerun.")
        else:
            print("- Status: live and operational.")
        print("- Next: run `myos brief --meeting-hours <n>` or `myos run-day --env-file <path>` daily.")
    finally:
        release_lock(conn, "go_live", lock_owner)
        conn.commit()


def cmd_metrics(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_items,
          (SELECT COUNT(*) FROM work_items WHERE status='open' AND risk_score >= ?) AS at_risk,
          (SELECT COUNT(*) FROM inbox_items WHERE status='new') AS inbox_new,
          (SELECT COUNT(*) FROM external_items) AS external_total,
          (SELECT COUNT(*) FROM event_log WHERE created_at >= datetime('now', ?)) AS recent_events
        """,
        (args.risk_threshold, f"-{args.days} days"),
    ).fetchone()

    mode_rows = conn.execute(
        """
        SELECT mode, COUNT(*) AS c
        FROM daily_logs
        WHERE created_at >= datetime('now', ?)
        GROUP BY mode
        ORDER BY c DESC
        """,
        (f"-{args.days} days",),
    ).fetchall()
    sync_rows = conn.execute(
        """
        SELECT connector, last_status, last_success_at
        FROM sync_state
        ORDER BY connector ASC
        """
    ).fetchall()
    commitment_rows = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome='completed_late' THEN 1 ELSE 0 END) AS late,
          SUM(CASE WHEN outcome='missed' THEN 1 ELSE 0 END) AS missed,
          SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS open_c
        FROM commitment_log
        """
    ).fetchone()
    print(f"KPI snapshot (last {args.days} days):")
    print(f"- open_items={rows['open_items']} at_risk={rows['at_risk']} inbox_new={rows['inbox_new']}")
    print(f"- external_total={rows['external_total']} recent_events={rows['recent_events']}")
    if mode_rows:
        modes = ", ".join(f"{r['mode']}={r['c']}" for r in mode_rows)
        print(f"- mode_distribution: {modes}")
    else:
        print("- mode_distribution: none")
    if sync_rows:
        statuses = ", ".join(f"{r['connector']}:{r['last_status']}" for r in sync_rows)
        print(f"- connector_status: {statuses}")
    else:
        print("- connector_status: none")
    print(
        "- commitment_health: "
        f"on_time={commitment_rows['on_time'] or 0}, "
        f"late={commitment_rows['late'] or 0}, "
        f"missed={commitment_rows['missed'] or 0}, "
        f"open={commitment_rows['open_c'] or 0}"
    )


def cmd_log_evidence(args: argparse.Namespace) -> None:
    conn = get_connection()
    filtered_impact = apply_privacy_filters(conn, args.impact)
    conn.execute(
        """
        INSERT INTO review_evidence (person, category, impact, artifact_link, privacy_level)
        VALUES (?, ?, ?, ?, ?)
        """,
        (args.person, args.category, filtered_impact, args.artifact_link, args.privacy),
    )
    conn.commit()
    print(f"Evidence logged for {args.person}.")


def cmd_review_evidence(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, person, category, impact, artifact_link, privacy_level, created_at
        FROM review_evidence
        WHERE (? = '' OR person = ?)
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (args.person, args.person, args.limit),
    ).fetchall()
    if not rows:
        print("No review evidence found.")
        return
    print("Review evidence:")
    for row in rows:
        link = row["artifact_link"] or "none"
        print(
            f"- #{row['id']} person={row['person']} category={row['category']} "
            f"privacy={row['privacy_level']} link={link}\n  impact={row['impact']}"
        )


def cmd_resolve_commitment(args: argparse.Namespace) -> None:
    conn = get_connection()
    wi = conn.execute(
        "SELECT id, due_date FROM work_items WHERE id = ?",
        (args.item,),
    ).fetchone()
    if not wi:
        print("Work item not found.")
        return
    due = wi["due_date"]
    outcome = args.outcome
    if args.outcome == "auto":
        if args.resolved_on and due and args.resolved_on > due:
            outcome = "completed_late"
        elif args.resolved_on and due and args.resolved_on <= due:
            outcome = "completed_on_time"
        else:
            outcome = "completed_on_time"

    cur = conn.execute(
        """
        UPDATE commitment_log
        SET resolved_on = ?, outcome = ?, notes = ?
        WHERE work_item_id = ? AND outcome = 'open'
        """,
        (args.resolved_on or date.today().isoformat(), outcome, args.notes, args.item),
    )
    if cur.rowcount == 0:
        # Backfill legacy commitment items that predate commitment_log.
        conn.execute(
            """
            INSERT INTO commitment_log (work_item_id, promised_on, due_on, resolved_on, outcome, notes)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
            """,
            (args.item, due, args.resolved_on or date.today().isoformat(), outcome, args.notes),
        )
    if outcome in ("completed_on_time", "completed_late"):
        conn.execute("UPDATE work_items SET status='done', updated_at=CURRENT_TIMESTAMP WHERE id = ?", (args.item,))
    elif outcome == "missed":
        conn.execute("UPDATE work_items SET risk_score = MIN(risk_score + 20, 100), updated_at=CURRENT_TIMESTAMP WHERE id = ?", (args.item,))
    conn.commit()
    print(f"Commitment #{args.item} resolved with outcome={outcome}.")


def cmd_weekly_review(args: argparse.Namespace) -> None:
    conn = get_connection()
    open_count = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='open'").fetchone()["c"]
    done_count = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='done'").fetchone()["c"]
    risk_count = conn.execute(
        "SELECT COUNT(*) AS c FROM work_items WHERE status='open' AND risk_score >= ?",
        (args.risk_threshold,),
    ).fetchone()["c"]
    evidence_count = conn.execute(
        "SELECT COUNT(*) AS c FROM review_evidence WHERE created_at >= datetime('now', ?)",
        (f"-{args.days} days",),
    ).fetchone()["c"]
    commitment = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome='completed_late' THEN 1 ELSE 0 END) AS late,
          SUM(CASE WHEN outcome='missed' THEN 1 ELSE 0 END) AS missed,
          SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS open_c
        FROM commitment_log
        """
    ).fetchone()
    print(f"Weekly review ({args.days}d window):")
    print(f"- open={open_count} done={done_count} at_risk={risk_count}")
    print(
        f"- commitments on_time={commitment['on_time'] or 0} "
        f"late={commitment['late'] or 0} missed={commitment['missed'] or 0} open={commitment['open_c'] or 0}"
    )
    print(f"- review evidence captured={evidence_count}")
    if risk_count > args.risk_alert:
        print("- Alert: risk load is high, run `myos stop-doing` and rebalance commitments.")
    if (commitment["missed"] or 0) > 0:
        print("- Alert: missed commitments detected; renegotiate deadlines and update owners.")


def cmd_launchd_install(args: argparse.Namespace) -> None:
    project_root = Path(__file__).resolve().parents[2]
    env_file = args.env_file or str(project_root / "data" / ".env.myos")
    env_file_q = str(Path(env_file).expanduser().resolve())
    project_q = shlex.quote(str(project_root))
    env_q = shlex.quote(str(env_file_q))
    sync_cmd = f"cd {project_q} && source .venv/bin/activate && myos sync --connector all --env-file {env_q}"
    pulse_cmd = (
        f"cd {project_q} && source .venv/bin/activate && "
        f"myos pulse --env-file {env_q} --interval-sec {int(args.interval_sec)} "
        f"--meeting-hours {float(args.meeting_hours)}"
    )
    autopilot_cmd = (
        f"cd {project_q} && source .venv/bin/activate && "
        f"myos autopilot --env-file {env_q} --interval-sec {int(args.autopilot_interval_sec)}"
    )
    (project_root / "data").mkdir(parents=True, exist_ok=True)
    target_dir = Path.home() / "Library" / "LaunchAgents"
    dst_sync = target_dir / "com.myos.sync.plist"
    dst_pulse = target_dir / "com.myos.pulse.plist"
    dst_autopilot = target_dir / "com.myos.autopilot.plist"

    sync_plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.myos.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(sync_cmd)}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>{args.interval_sec}</integer>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'sync.log'))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'sync.err.log'))}</string>
</dict>
</plist>
"""
    pulse_plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.myos.pulse</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(pulse_cmd)}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'pulse.log'))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'pulse.err.log'))}</string>
</dict>
</plist>
"""
    autopilot_plist = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>com.myos.autopilot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{xml_escape(autopilot_cmd)}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'autopilot.log'))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(project_root / 'data' / 'autopilot.err.log'))}</string>
</dict>
</plist>
"""

    print("Launchd plan:")
    print(f"- write {dst_sync}")
    print(f"- write {dst_pulse}")
    if args.autopilot:
        print(f"- write {dst_autopilot}")
    print(f"- env file for sync: {env_file_q}")
    print(f"- env file for pulse: {env_file_q}")
    if args.autopilot:
        print(f"- env file for autopilot: {env_file_q}")
    print(f"- load agents: {args.load}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to execute.")
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    dst_sync.write_text(sync_plist)
    dst_pulse.write_text(pulse_plist)
    if args.autopilot:
        dst_autopilot.write_text(autopilot_plist)
    print("Copied launchd files.")
    if args.load:
        subprocess.run(["launchctl", "unload", str(dst_sync)], check=False)
        subprocess.run(["launchctl", "unload", str(dst_pulse)], check=False)
        if args.autopilot:
            subprocess.run(["launchctl", "unload", str(dst_autopilot)], check=False)
        subprocess.run(["launchctl", "load", str(dst_sync)], check=False)
        subprocess.run(["launchctl", "load", str(dst_pulse)], check=False)
        if args.autopilot:
            subprocess.run(["launchctl", "load", str(dst_autopilot)], check=False)
        print("Loaded launch agents.")


def cmd_launchd_uninstall(args: argparse.Namespace) -> None:
    target_dir = Path.home() / "Library" / "LaunchAgents"
    dst_sync = target_dir / "com.myos.sync.plist"
    dst_pulse = target_dir / "com.myos.pulse.plist"
    dst_autopilot = target_dir / "com.myos.autopilot.plist"
    print("Launchd uninstall plan:")
    print(f"- remove {dst_sync}")
    print(f"- remove {dst_pulse}")
    print(f"- remove {dst_autopilot}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to execute.")
        return
    if dst_sync.exists():
        subprocess.run(["launchctl", "unload", str(dst_sync)], check=False, capture_output=True, text=True)
        dst_sync.unlink()
    if dst_pulse.exists():
        subprocess.run(["launchctl", "unload", str(dst_pulse)], check=False, capture_output=True, text=True)
        dst_pulse.unlink()
    if dst_autopilot.exists():
        subprocess.run(["launchctl", "unload", str(dst_autopilot)], check=False, capture_output=True, text=True)
        dst_autopilot.unlink()
    print("Launch agents removed.")


def cmd_activate(args: argparse.Namespace) -> None:
    if args.env_file:
        loaded = load_env_file(args.env_file)
        print(f"Loaded {loaded} vars from {args.env_file}")
    print("Activation flow: onboard -> go-live -> optional launchd install")
    cmd_onboard(argparse.Namespace())
    print()
    cmd_go_live(
        argparse.Namespace(
            connector=args.connector,
            env_file=args.env_file,
            external_limit=args.external_limit,
        )
    )
    if args.install_launchd:
        print()
        cmd_launchd_install(
            argparse.Namespace(
                apply=True,
                load=args.load_launchd,
                env_file=args.env_file,
                interval_sec=1800,
                meeting_hours=0.0,
                autopilot=False,
                autopilot_interval_sec=900,
            )
        )


def cmd_launchd_status(_: argparse.Namespace) -> None:
    labels = ["com.myos.sync", "com.myos.pulse", "com.myos.autopilot"]
    print("Launchd status:")
    for label in labels:
        proc = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            print(f"- {label}: loaded")
        else:
            print(f"- {label}: not_loaded")


def cmd_start(args: argparse.Namespace) -> None:
    print("Starting MYOS runtime: activate -> launchd status -> sanity")
    cmd_activate(
        argparse.Namespace(
            env_file=args.env_file,
            connector=args.connector,
            external_limit=args.external_limit,
            install_launchd=args.install_launchd,
            load_launchd=args.load_launchd,
        )
    )
    print()
    cmd_launchd_status(argparse.Namespace())
    print()
    cmd_sanity(argparse.Namespace(strict=False, report_dir=args.report_dir))


def cmd_stop(args: argparse.Namespace) -> None:
    print("Stopping MYOS runtime: unload/remove launchd -> status")
    cmd_launchd_uninstall(argparse.Namespace(apply=True))
    print()
    cmd_launchd_status(argparse.Namespace())


def cmd_dashboard(args: argparse.Namespace) -> None:
    conn = get_connection()
    if args.once:
        output_path = Path(args.output_html) if args.output_html else (Path(__file__).resolve().parents[2] / "data" / "dashboard.html")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_dashboard_html(conn, report_dir=args.report_dir))
        print(f"Dashboard snapshot written: {output_path}")
        return
    print(f"Serving dashboard at http://{args.host}:{args.port}")
    serve_dashboard(conn, host=args.host, port=args.port, report_dir=args.report_dir)


def cmd_sanity(args: argparse.Namespace) -> None:
    conn = get_connection()
    checks: list[tuple[str, bool, str]] = []

    db_ok = conn.execute("SELECT 1").fetchone() is not None
    checks.append(("db_connection", db_ok, "SQLite connection and basic query"))

    required_tables = [
        "inbox_items",
        "work_items",
        "external_items",
        "sync_state",
        "review_evidence",
        "commitment_log",
    ]
    existing = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = [t for t in required_tables if t not in existing]
    checks.append(("schema_tables", len(missing) == 0, f"missing={','.join(missing) if missing else 'none'}"))

    sync_rows = conn.execute("SELECT connector, last_status FROM sync_state").fetchall()
    if not sync_rows:
        checks.append(("connector_sync_state", False, "no connector state yet"))
    else:
        bad = [r["connector"] for r in sync_rows if r["last_status"] == "error"]
        checks.append(("connector_sync_state", len(bad) == 0, f"errors={','.join(bad) if bad else 'none'}"))

    inbox_new = conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE status='new'").fetchone()["c"]
    open_items = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='open'").fetchone()["c"]
    checks.append(("load_levels", True, f"inbox_new={inbox_new}, open_items={open_items}"))

    report_dir = Path(args.report_dir) if args.report_dir else Path(__file__).resolve().parents[2] / "data" / "reports"
    latest_reports = sorted(report_dir.glob("daily-brief-*.md"), reverse=True)[:1] if report_dir.exists() else []
    checks.append(("daily_report", len(latest_reports) > 0, f"latest={latest_reports[0].name if latest_reports else 'none'}"))

    all_pass = True
    print("Sanity check:")
    for name, ok, detail in checks:
        status = "PASS" if ok else "WARN"
        print(f"- {status} {name}: {detail}")
        if not ok and name in ("db_connection", "schema_tables"):
            all_pass = False

    if args.strict and any(not ok for _, ok, _ in checks):
        raise SystemExit(1)
    if all_pass:
        print("Sanity complete: core checks passed.")
    else:
        print("Sanity complete: core issues found.")


def cmd_runbook(args: argparse.Namespace) -> None:
    print("MYOS Operational Runbook")
    print("\nDaily startup")
    print("1) myos sanity")
    print("2) myos run-day --env-file <path> --meeting-hours <n>")
    print("3) myos brief --meeting-hours <n>")
    print("4) myos dashboard --once --output-html ./data/dashboard.html")
    print("\nMidday")
    print("- myos at-risk")
    print("- myos stop-doing --capacity <n> --deep-budget <n>")
    print("\nEnd of day")
    print("- myos close-day --mode <maker|hybrid|meeting-heavy|recovery> --note \"...\"")
    print("- myos report --meeting-hours <n>")
    print("\nWeekly")
    print("- myos weekly-review --days 7")
    print("- myos metrics --days 7")
    print("- myos review-evidence --person self")
    print("\nGo-live activation")
    print("- myos activate --env-file <path> --install-launchd --load-launchd")
    if args.short:
        return
    print("\nTroubleshooting")
    print("- myos onboard")
    print("- myos doctor")
    print("- myos sync --connector all --env-file <path>")
    print("- myos launchd-uninstall --apply (if launch agent reset needed)")


def cmd_cleanup(args: argparse.Namespace) -> None:
    conn = get_connection()
    stale_rows = conn.execute(
        """
        SELECT id, title
        FROM work_items
        WHERE status='open' AND created_at < datetime('now', ?)
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (f"-{args.days} days", args.limit),
    ).fetchall()
    archived = 0
    for row in stale_rows:
        conn.execute(
            "UPDATE work_items SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id = ?",
            (row["id"],),
        )
        archived += 1
    retention = _cleanup_policy_retention(conn)
    conn.commit()
    print(f"Cleanup complete. Archived {archived} stale open items.")
    print(
        f"Policy retention cleanup: media_deleted={retention['media']} "
        f"evidence_deleted={retention['evidence']} conversation_turns_deleted={retention['conversation_turns']}"
    )


def cmd_renegotiate(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, title, due_date, risk_score, owner
        FROM work_items
        WHERE status='open'
          AND kind IN ('commitment', 'risk')
          AND due_date IS NOT NULL
          AND due_date <= date('now', ?)
        ORDER BY due_date ASC, risk_score DESC
        LIMIT ?
        """,
        (f"+{args.days_ahead} days", args.limit),
    ).fetchall()
    if not rows:
        print("No commitments requiring renegotiation in window.")
        return
    print("Renegotiation candidates:")
    for row in rows:
        owner = row["owner"] or "stakeholder"
        suggested = args.default_extension_days
        print(
            f"- #{row['id']} {row['title']} (due={row['due_date']}, risk={row['risk_score']})\n"
            f"  suggested message: \"Hi {owner}, this item is at risk. Proposing a {suggested}-day extension "
            f"or scope reduction. Can we confirm priority and deadline?\""
        )
    append_event(
        conn,
        "renegotiate_review",
        "work_item",
        None,
        json.dumps({"candidates": len(rows), "days_ahead": args.days_ahead}, ensure_ascii=True),
    )
    conn.commit()


def cmd_next_action(args: argparse.Namespace) -> None:
    conn = get_connection()
    mode = detect_mode(args.meeting_hours)
    risk = conn.execute(
        """
        SELECT id, title, risk_score, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 1
        """,
        (args.risk_threshold,),
    ).fetchone()
    waiting = conn.execute(
        """
        SELECT id, title, owner, due_date
        FROM work_items
        WHERE status='open' AND owner IS NOT NULL AND kind IN ('commitment', 'risk')
        ORDER BY COALESCE(due_date, '9999-12-31') ASC
        LIMIT 1
        """
    ).fetchone()
    deep = conn.execute(
        """
        SELECT id, title, kind, risk_score
        FROM work_items
        WHERE status='open' AND kind IN ('task', 'decision', 'commitment')
        ORDER BY priority ASC, risk_score DESC, created_at ASC
        LIMIT 1
        """
    ).fetchone()

    print(f"Next action recommendation (mode={mode}):")
    if mode == "meeting-heavy":
        if waiting:
            print(
                f"- Nudge owner: #{waiting['id']} {waiting['title']} (owner={waiting['owner']}, due={waiting['due_date'] or 'none'})"
            )
        elif risk:
            print(
                f"- Renegotiate risk item: #{risk['id']} {risk['title']} (risk={risk['risk_score']}, due={risk['due_date'] or 'none'})"
            )
        elif deep:
            print(f"- Keep one tiny win only: #{deep['id']} {deep['title']}")
        else:
            print("- No open items. Capture and triage first.")
        return

    if risk:
        print(
            f"- Reduce top risk now: #{risk['id']} {risk['title']} (risk={risk['risk_score']}, due={risk['due_date'] or 'none'})"
        )
    elif deep:
        print(f"- Focus block target: #{deep['id']} {deep['title']} (kind={deep['kind']})")
    else:
        print("- No open items. Capture and triage first.")


def cmd_snapshot(args: argparse.Namespace) -> None:
    conn = get_connection()
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_items,
          (SELECT COUNT(*) FROM work_items WHERE status='done') AS done_items,
          (SELECT COUNT(*) FROM work_items WHERE status='archived') AS archived_items,
          (SELECT COUNT(*) FROM work_items WHERE status='open' AND risk_score >= ?) AS at_risk,
          (SELECT COUNT(*) FROM inbox_items WHERE status='new') AS inbox_new
        """,
        (args.risk_threshold,),
    ).fetchone()
    top_risk = conn.execute(
        """
        SELECT id, title, kind, risk_score, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT ?
        """,
        (args.risk_threshold, args.limit),
    ).fetchall()
    connectors = conn.execute(
        """
        SELECT connector, last_status, last_success_at, last_error
        FROM sync_state
        ORDER BY connector ASC
        """
    ).fetchall()
    commitments = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome='completed_late' THEN 1 ELSE 0 END) AS late,
          SUM(CASE WHEN outcome='missed' THEN 1 ELSE 0 END) AS missed,
          SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS open_c
        FROM commitment_log
        """
    ).fetchone()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "open_items": counts["open_items"],
            "done_items": counts["done_items"],
            "archived_items": counts["archived_items"],
            "at_risk": counts["at_risk"],
            "inbox_new": counts["inbox_new"],
        },
        "top_risk": [
            {
                "id": r["id"],
                "title": r["title"],
                "kind": r["kind"],
                "risk_score": r["risk_score"],
                "due_date": r["due_date"],
            }
            for r in top_risk
        ],
        "connectors": [
            {
                "name": r["connector"],
                "status": r["last_status"],
                "last_success_at": r["last_success_at"],
                "last_error": r["last_error"],
            }
            for r in connectors
        ],
        "commitments": {
            "on_time": commitments["on_time"] or 0,
            "late": commitments["late"] or 0,
            "missed": commitments["missed"] or 0,
            "open": commitments["open_c"] or 0,
        },
    }

    body = json.dumps(payload, indent=2, ensure_ascii=True)
    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body + "\n")
        print(f"Snapshot written: {out_path}")
        return
    print(body)


def cmd_morning(args: argparse.Namespace) -> None:
    cmd_run_day(
        argparse.Namespace(
            env_file=args.env_file,
            connector="all",
            meeting_hours=args.meeting_hours,
            external_limit=100,
            media_limit=30,
            min_confidence=0.65,
            risk_threshold=60,
            capacity=8,
            deep_budget=3,
            keep_risk=60,
            stop_limit=10,
            output_dir="",
        )
    )


def cmd_now(args: argparse.Namespace) -> None:
    cmd_next_action(argparse.Namespace(meeting_hours=args.meeting_hours, risk_threshold=60))


def cmd_end(_: argparse.Namespace) -> None:
    cmd_close_day(argparse.Namespace(mode="hybrid", note="end-of-day quick close"))
    cmd_report(argparse.Namespace(meeting_hours=0.0, risk_threshold=60, output_dir=""))


def cmd_weekly(_: argparse.Namespace) -> None:
    cmd_orchestrate(
        argparse.Namespace(
            workflow="weekly",
            env_file="",
            connector="all",
            meeting_hours=0.0,
            external_limit=100,
            media_limit=30,
            min_confidence=0.65,
            risk_threshold=60,
            capacity=8,
            deep_budget=3,
            keep_risk=60,
            stop_limit=10,
            output_dir="",
        )
    )


def cmd_live(args: argparse.Namespace) -> None:
    cmd_activate(
        argparse.Namespace(
            env_file=args.env_file,
            connector="all",
            external_limit=100,
            install_launchd=args.install_launchd,
            load_launchd=args.load_launchd,
        )
    )


def cmd_health(_: argparse.Namespace) -> None:
    cmd_sanity(argparse.Namespace(strict=False, report_dir=""))
    print()
    cmd_doctor(argparse.Namespace())


def cmd_ui(args: argparse.Namespace) -> None:
    cmd_dashboard(
        argparse.Namespace(
            host="127.0.0.1",
            port=args.port,
            report_dir="",
            once=False,
            output_html="",
        )
    )


def cmd_orchestrate(args: argparse.Namespace) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO workflow_runs (workflow_name, status) VALUES (?, 'running')",
        (args.workflow,),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.commit()

    def step(name: str, fn, fn_args: argparse.Namespace) -> None:
        conn.execute(
            "INSERT INTO workflow_steps (workflow_run_id, step_name, status) VALUES (?, ?, 'running')",
            (run_id, name),
        )
        step_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
        try:
            result = fn(fn_args)
            status = "completed"
            details = "ok"
            if isinstance(result, dict):
                status = result.get("status", "completed")
                details = result.get("details", "ok")
                if status not in {"completed", "skipped"}:
                    status = "completed"
            conn.execute(
                "UPDATE workflow_steps SET status='completed', details=? WHERE id=?",
                (details, step_id),
            )
            if status == "skipped":
                conn.execute(
                    "UPDATE workflow_steps SET status='skipped' WHERE id=?",
                    (step_id,),
                )
            conn.commit()
        except Exception as exc:
            conn.execute(
                "UPDATE workflow_steps SET status='failed', details=? WHERE id=?",
                (str(exc), step_id),
            )
            conn.execute(
                "UPDATE workflow_runs SET status='failed', finished_at=CURRENT_TIMESTAMP, summary=? WHERE id=?",
                (f"failed at step {name}: {exc}", run_id),
            )
            conn.commit()
            raise

    try:
        if args.workflow == "daily":
            step(
                "run_day",
                cmd_run_day,
                argparse.Namespace(
                    env_file=args.env_file,
                    connector=args.connector,
                    meeting_hours=args.meeting_hours,
                    external_limit=args.external_limit,
                    media_limit=args.media_limit,
                    min_confidence=args.min_confidence,
                    risk_threshold=args.risk_threshold,
                    capacity=args.capacity,
                    deep_budget=args.deep_budget,
                    keep_risk=args.keep_risk,
                    stop_limit=args.stop_limit,
                    output_dir=args.output_dir,
                ),
            )
        elif args.workflow == "weekly":
            step("weekly_review", cmd_weekly_review, argparse.Namespace(days=7, risk_threshold=args.risk_threshold, risk_alert=5))
            step("metrics", cmd_metrics, argparse.Namespace(days=7, risk_threshold=args.risk_threshold))
            step("report", cmd_report, argparse.Namespace(meeting_hours=args.meeting_hours, risk_threshold=args.risk_threshold, output_dir=args.output_dir))
        elif args.workflow == "incident":
            step("at_risk", cmd_at_risk, argparse.Namespace(threshold=args.risk_threshold, limit=20))
            step(
                "renegotiate",
                cmd_renegotiate,
                argparse.Namespace(days_ahead=2, default_extension_days=3, limit=20),
            )
            step("next_action", cmd_next_action, argparse.Namespace(meeting_hours=args.meeting_hours, risk_threshold=args.risk_threshold))
        else:
            raise ValueError(f"Unknown workflow: {args.workflow}")

        stats = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_c,
              SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped_c,
              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_c
            FROM workflow_steps
            WHERE workflow_run_id = ?
            """,
            (run_id,),
        ).fetchone()
        summary = (
            f"completed={stats['completed_c'] or 0}, "
            f"skipped={stats['skipped_c'] or 0}, "
            f"failed={stats['failed_c'] or 0}"
        )
        conn.execute(
            "UPDATE workflow_runs SET status='completed', finished_at=CURRENT_TIMESTAMP, summary=? WHERE id=?",
            (summary, run_id),
        )
        conn.commit()
        print(f"Workflow complete: {args.workflow} (run_id={run_id}) {summary}")
    except Exception as exc:
        print(f"Workflow failed: {args.workflow} (run_id={run_id}) error={exc}")
        raise SystemExit(1)


def cmd_workflow_runs(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, workflow_name, status, started_at, finished_at, summary
        FROM workflow_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No workflow runs found.")
        return
    print("Workflow runs:")
    for row in rows:
        print(
            f"- run_id={row['id']} workflow={row['workflow_name']} status={row['status']} "
            f"started={row['started_at']} finished={row['finished_at'] or 'running'} summary={row['summary'] or ''}"
        )


def cmd_policy(args: argparse.Namespace) -> None:
    conn = get_connection()
    if args.set:
        if "=" not in args.set:
            print("Invalid --set format. Use KEY=VALUE.")
            raise SystemExit(1)
        key, value = args.set.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            print("Policy key cannot be empty.")
            raise SystemExit(1)
        conn.execute(
            """
            INSERT INTO assistant_policies (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        conn.commit()
        print(f"Policy updated: {key}={value}")
        return
    print("Policy settings:")
    for key, value in sorted(get_policy_map(conn).items()):
        print(f"- {key}={value}")


def cmd_queue_add(args: argparse.Namespace) -> None:
    conn = get_connection()
    payload = {}
    if args.payload:
        payload = json.loads(args.payload)
    conn.execute(
        """
        INSERT INTO workflow_queue (workflow_name, payload_json, status)
        VALUES (?, ?, 'queued')
        """,
        (args.workflow, json.dumps(payload, ensure_ascii=True)),
    )
    job_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.commit()
    print(f"Queued workflow job #{job_id}: {args.workflow}")


def cmd_worker(args: argparse.Namespace) -> None:
    conn = get_connection()
    candidates = conn.execute(
        """
        SELECT id, workflow_name, payload_json
        FROM workflow_queue
        WHERE status='queued'
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not candidates:
        print("Worker: no queued jobs.")
        return
    processed = 0
    for row in candidates:
        job_id = int(row["id"])
        workflow = row["workflow_name"]
        # Atomic compare-and-claim: only the worker whose UPDATE flips queued->running
        # processes this job. Two concurrent workers can no longer double-run it.
        claim = conn.execute(
            "UPDATE workflow_queue SET status='running', started_at=CURRENT_TIMESTAMP WHERE id = ? AND status='queued'",
            (job_id,),
        )
        conn.commit()
        if claim.rowcount != 1:
            continue  # already claimed by another worker
        processed += 1
        try:
            payload = json.loads(row["payload_json"] or "{}")
            cmd_orchestrate(
                argparse.Namespace(
                    workflow=workflow,
                    env_file=str(payload.get("env_file", "")),
                    connector=str(payload.get("connector", "all")),
                    meeting_hours=float(payload.get("meeting_hours", 0.0)),
                    external_limit=int(payload.get("external_limit", 100)),
                    media_limit=int(payload.get("media_limit", 30)),
                    min_confidence=float(payload.get("min_confidence", 0.65)),
                    risk_threshold=int(payload.get("risk_threshold", 60)),
                    capacity=int(payload.get("capacity", 8)),
                    deep_budget=int(payload.get("deep_budget", 3)),
                    keep_risk=int(payload.get("keep_risk", 60)),
                    stop_limit=int(payload.get("stop_limit", 10)),
                    output_dir=str(payload.get("output_dir", "")),
                )
            )
            conn.execute(
                "UPDATE workflow_queue SET status='completed', finished_at=CURRENT_TIMESTAMP, last_error='' WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            print(f"Worker completed job #{job_id} ({workflow})")
        except Exception as exc:
            conn.execute(
                """
                UPDATE workflow_queue
                SET status='failed', finished_at=CURRENT_TIMESTAMP, last_error=?
                WHERE id = ?
                """,
                (str(exc), job_id),
            )
            conn.commit()
            print(f"Worker failed job #{job_id} ({workflow}): {exc}")
    if processed == 0:
        print("Worker: all queued jobs were already claimed by another worker.")


def cmd_cutover_check(_: argparse.Namespace) -> None:
    conn = get_connection()
    required = {
        "jira": ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"],
        "github": ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"],
        "confluence": ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        "aha": ["AHA_BASE_URL", "AHA_API_TOKEN"],
    }
    print("Cutover readiness:")
    ready = 0
    for name, keys in required.items():
        missing = [k for k in keys if not os.getenv(k)]
        if missing:
            print(f"- {name}: MISSING {', '.join(missing)}")
            continue
        state = conn.execute(
            "SELECT last_status, last_success_at FROM sync_state WHERE connector = ?",
            (name,),
        ).fetchone()
        if not state:
            print(f"- {name}: CREDS_READY sync=never")
            continue
        print(f"- {name}: CREDS_READY sync={state['last_status']} last_success={state['last_success_at']}")
        ready += 1
    print(f"Connectors credential-ready: {ready}/{len(required)}")
    if ready == len(required):
        print("Cutover check: READY for go-live.")
    else:
        print("Cutover check: NOT_READY. Fill env vars and rerun.")


def cmd_uat(args: argparse.Namespace) -> None:
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM work_items WHERE created_at >= datetime('now', ?)",
        (f"-{args.days} days",),
    ).fetchone()["c"]
    hi_risk = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM work_items
        WHERE created_at >= datetime('now', ?) AND risk_score >= ?
        """,
        (f"-{args.days} days", args.risk_threshold),
    ).fetchone()["c"]
    commitments = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome IN ('completed_on_time','completed_late','missed') THEN 1 ELSE 0 END) AS resolved
        FROM commitment_log
        WHERE COALESCE(resolved_on, promised_on, due_on) >= date('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()
    interventions = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM event_log
        WHERE event_type IN ('stop_doing_review', 'renegotiate_review')
          AND created_at >= datetime('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()["c"]
    activity = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM event_log
        WHERE created_at >= datetime('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()["c"]
    backlog_new = conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE status='new'").fetchone()["c"]
    on_time = commitments["on_time"] or 0
    resolved = commitments["resolved"] or 0
    acceptance_rate = (100.0 * on_time / resolved) if resolved else 0.0
    intervention_rate = (100.0 * interventions / activity) if activity else 0.0
    risk_focus = (100.0 * hi_risk / total) if total else 0.0

    print(f"UAT quality snapshot ({args.days}d):")
    print(f"- throughput: work_items={total} backlog_new={backlog_new}")
    print(
        f"- prioritization_focus: high_risk_items={hi_risk}/{total} "
        f"({risk_focus:.1f}%) threshold={args.risk_threshold}"
    )
    print(
        f"- commitment_reliability: on_time={on_time}/{resolved} "
        f"({acceptance_rate:.1f}%)"
    )
    print(
        f"- intervention_signal: interventions={interventions}/{activity} "
        f"({intervention_rate:.1f}%)"
    )
    if backlog_new > args.backlog_warn:
        print("- ALERT: inbox backlog too high; run `myos triage`.")
    if acceptance_rate < args.acceptance_warn and resolved >= args.min_sample:
        print("- ALERT: acceptance rate low; revisit prioritization and renegotiation cadence.")
    if risk_focus < args.risk_focus_warn and total >= args.min_sample:
        print("- ALERT: risk focus too low; raise risk threshold tuning or adjust inference.")


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 60
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * max(0.0, min(1.0, pct))))
    return int(ordered[idx])


def cmd_tune(args: argparse.Namespace) -> None:
    conn = get_connection()
    risk_rows = conn.execute(
        """
        SELECT risk_score
        FROM work_items
        WHERE created_at >= datetime('now', ?) AND status='open'
        ORDER BY risk_score ASC
        """,
        (f"-{args.days} days",),
    ).fetchall()
    risk_scores = [int(r["risk_score"]) for r in risk_rows]
    suggested_risk_threshold = max(45, min(85, _percentile(risk_scores, 0.75)))

    commitments = conn.execute(
        """
        SELECT
          SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
          SUM(CASE WHEN outcome IN ('completed_on_time','completed_late','missed') THEN 1 ELSE 0 END) AS resolved
        FROM commitment_log
        WHERE COALESCE(resolved_on, promised_on, due_on) >= date('now', ?)
        """,
        (f"-{args.days} days",),
    ).fetchone()
    on_time = int(commitments["on_time"] or 0)
    resolved = int(commitments["resolved"] or 0)
    acceptance_rate = (100.0 * on_time / resolved) if resolved else 70.0
    suggested_acceptance_warn = max(50.0, min(90.0, acceptance_rate - 10.0))

    backlog_new = int(conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE status='new'").fetchone()["c"])
    open_items = int(conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='open'").fetchone()["c"])
    suggested_backlog_warn = max(8, min(40, int((open_items * 0.5) + 5)))

    hi_risk = int(
        conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM work_items
            WHERE created_at >= datetime('now', ?) AND risk_score >= ?
            """,
            (f"-{args.days} days", suggested_risk_threshold),
        ).fetchone()["c"]
    )
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM work_items WHERE created_at >= datetime('now', ?)",
            (f"-{args.days} days",),
        ).fetchone()["c"]
    )
    risk_focus_pct = (100.0 * hi_risk / total) if total else 25.0
    suggested_risk_focus_warn = max(15.0, min(45.0, risk_focus_pct - 5.0))

    print(f"Tuning recommendations ({args.days}d window):")
    print(f"- current_state: open_items={open_items} backlog_new={backlog_new} resolved_commitments={resolved}")
    print(f"- suggested risk_threshold={suggested_risk_threshold}")
    print(f"- suggested backlog_warn={suggested_backlog_warn}")
    print(f"- suggested acceptance_warn={suggested_acceptance_warn:.1f}")
    print(f"- suggested risk_focus_warn={suggested_risk_focus_warn:.1f}")
    print(
        "- suggested uat command: "
        f"myos uat --days {args.days} "
        f"--risk-threshold {suggested_risk_threshold} "
        f"--backlog-warn {suggested_backlog_warn} "
        f"--acceptance-warn {suggested_acceptance_warn:.1f} "
        f"--risk-focus-warn {suggested_risk_focus_warn:.1f}"
    )

    if args.apply_policy:
        updates = {
            "uat_risk_threshold": str(suggested_risk_threshold),
            "uat_backlog_warn": str(suggested_backlog_warn),
            "uat_acceptance_warn": f"{suggested_acceptance_warn:.1f}",
            "uat_risk_focus_warn": f"{suggested_risk_focus_warn:.1f}",
        }
        for key, value in updates.items():
            conn.execute(
                """
                INSERT INTO assistant_policies (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()
        print("Applied recommendations into policy keys: uat_*")


def cmd_delegate(args: argparse.Namespace) -> None:
    conn = get_connection()
    target = getattr(args, "to", "").strip().lower()
    if target and target not in ("local",):
        result = assistant.delegate_to_agent(conn, target, args.objective)
        if result.get("error"):
            print(f"Delegation failed: {result['error']}")
            raise SystemExit(1)
        print(result.get("summary", "Delegated."))
        for aid in result.get("proposed_action_ids", []):
            print(f"- proposed action #{aid} (review with `myos approve --list`)")
        return
    constraints = {"mode": args.mode, "max_actions": args.max_actions}
    if args.constraint:
        constraints["constraints"] = args.constraint
    objective = apply_privacy_filters(conn, args.objective)
    context = apply_privacy_filters(conn, args.context)
    analogies = _agent_analogies(conn, f"{objective} {context}", limit=args.analogy_limit)
    plan, actions, provider = _ai_reason_artifacts(
        conn,
        objective=objective,
        context=context,
        analogies=analogies,
        purpose="delegate",
    )
    actions = actions[: args.max_actions]

    conn.execute(
        """
        INSERT INTO agent_tasks (objective, context, constraints_json, priority, status)
        VALUES (?, ?, ?, ?, 'open')
        """,
        (objective, context, json.dumps(constraints, ensure_ascii=True), args.priority),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        """
        INSERT INTO agent_runs (agent_task_id, agent_name, provider, status, plan_json, summary, finished_at)
        VALUES (?, 'assistant_core_v1', ?, 'completed', ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            task_id,
            provider,
            json.dumps(plan, ensure_ascii=True),
            f"Created {len(plan)} plan steps and {len(actions)} proposed actions.",
        ),
    )
    for score, source, content in analogies:
        conn.execute(
            """
            INSERT INTO agent_observations (agent_task_id, observation_type, content, confidence)
            VALUES (?, 'analogy', ?, ?)
            """,
            (task_id, f"{source}: {content}", min(0.95, max(0.55, score))),
        )
    for action in actions:
        # Redact here: cmd_delegate bypasses enqueue_proposal so redaction must happen
        # at the call site — enqueue_proposal already redacts when called from the
        # backends, this INSERT must match that protection (review R4-2).
        conn.execute(
            """
            INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, requires_approval)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                action["action_type"],
                apply_privacy_filters(conn, str(action["title"]))[:500],
                json.dumps(redact_obj(conn, action["payload"]), ensure_ascii=True),
                action["requires_approval"],
            ),
        )
    append_event(
        conn,
        "agent_delegate",
        "agent_task",
        task_id,
        json.dumps({"actions": len(actions), "analogies": len(analogies)}, ensure_ascii=True),
    )
    conn.commit()

    print(f"Delegated task #{task_id}: {objective}")
    print("Plan:")
    for idx, step in enumerate(plan, start=1):
        print(f"{idx}. {step['step']}: {step['detail']}")
    print("Proposed actions:")
    action_rows = conn.execute(
        "SELECT id, action_type, title, status, requires_approval FROM agent_actions WHERE agent_task_id=? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    for row in action_rows:
        approval = "approval_required" if row["requires_approval"] else "safe_local"
        print(f"- action #{row['id']} [{row['action_type']}] {row['title']} ({approval}, status={row['status']})")
    if analogies:
        print("Analogies:")
        for score, source, content in analogies[:3]:
            snippet = content if len(content) <= 120 else content[:117] + "..."
            print(f"- {source} score={score:.3f}: {snippet}")


# Paths a harnessed-agent patch may NEVER touch — editing these would let an
# approved diff disable the autonomy gate or hijack hooks on the next run (#4).
def cmd_action_provider(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        request = _read_provider_stdin()
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        safety = request.get("safety", {})
        if not isinstance(safety, dict):
            safety = {}
        approved = bool(safety.get("approved"))
        action_type = str(request.get("action_type", ""))
        target = str(payload.get("target") or payload.get("target_type") or "outbox").lower()
        title = apply_privacy_filters(conn, str(request.get("title") or "Assistant action"))
        body = apply_privacy_filters(conn, _provider_body(payload))
        if not body:
            raise ValueError("action payload does not include draft/body/text")
        agent_action_id = request.get("action_id")
        agent_action_id = int(agent_action_id) if agent_action_id is not None else None

        target_ref = str(payload.get("issue_key") or payload.get("issue_number") or payload.get("pr_number") or "draft")
        if not args.execute:
            outbox_id = _outbox_write(
                conn,
                agent_action_id=agent_action_id,
                provider="builtin",
                target_type=target,
                target_ref=target_ref,
                title=title,
                body=body,
                status="drafted",
                payload=payload,
            )
            conn.commit()
            print(
                json.dumps(
                    {"status": "drafted", "outbox_id": outbox_id, "target": _provider_target_summary(payload)},
                    ensure_ascii=True,
                )
            )
            return

        if not approved:
            raise PermissionError("approved action required for --execute")
        if action_type != "draft_external_update":
            raise ValueError(f"unsupported executable action_type={action_type}")

        outbox_id = _outbox_write(
            conn,
            agent_action_id=agent_action_id,
            provider="builtin",
            target_type=target,
            target_ref=target_ref,
            title=title,
            body=body,
            status="pending_execute",
            payload=payload,
        )
        conn.commit()
        if target == "jira":
            target_ref = str(payload.get("issue_key") or "")
            response = _post_jira_comment(target_ref, body)
        elif target == "github":
            target_ref = str(payload.get("issue_number") or payload.get("pr_number") or "")
            response = _post_github_comment(payload, body)
        else:
            raise ValueError("execute target must be jira or github")
        conn.execute(
            "UPDATE action_outbox SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?",
            (outbox_id,),
        )
        conn.commit()
        print(json.dumps({"status": "sent", "outbox_id": outbox_id, "provider_response": response}, ensure_ascii=True))
    except Exception as exc:
        conn.rollback()
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True))
        raise SystemExit(1)


def cmd_act(args: argparse.Namespace) -> None:
    conn = get_connection()
    if args.list:
        rows = conn.execute(
            """
            SELECT id, agent_task_id, action_type, title, status, requires_approval, payload_json
            FROM agent_actions
            WHERE (? IS NULL OR agent_task_id = ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (args.task, args.task, args.limit),
        ).fetchall()
        if not rows:
            print("No agent actions found.")
            return
        print("Agent actions:")
        for row in rows:
            approval = "approval_required" if row["requires_approval"] else "safe_local"
            print(f"- action #{row['id']} task=#{row['agent_task_id']} [{row['action_type']}] {row['title']} status={row['status']} {approval}")
            payload = json.loads(row["payload_json"] or "{}")
            print(f"  target: {_provider_target_summary(payload)}")
            preview = payload.get("draft") or payload.get("text")
            if preview:
                snippet = str(preview) if len(str(preview)) <= 180 else str(preview)[:177] + "..."
                print(f"  preview: {snippet}")
        return

    if args.action is None:
        print("Provide --action ID or use --list.")
        raise SystemExit(1)
    # Single approve/execute core lives in execution.approve_and_execute (refactor #12);
    # cmd_act just maps the structured outcome to the CLI's prints + exit codes.
    res = approve_and_execute(conn, args.action, do_approve=args.approve, execute=args.execute)
    code = res["code"]
    if code == "not_found":
        print("Agent action not found.")
        raise SystemExit(1)
    if res["approved"]:
        print(f"Approved action #{args.action}.")
    if code == "approved_only":
        return
    if code == "noop":
        if args.approve:
            print(f"Action #{args.action} is already {res['status']}; nothing to approve.")
        else:
            print(f"Action #{args.action} status={res['status']}. Use --approve and/or --execute.")
        return
    if code == "needs_approval":
        print("Action requires approval first. Re-run with --approve --execute.")
        raise SystemExit(1)
    if code == "already_executed":
        print(f"Action #{args.action} already executed.")
        return
    if code == "already_handled":
        print(f"Action #{args.action} is already being handled.")
        return
    if code == "failed":
        print(f"Action #{args.action} failed: {res['result']}")
        raise SystemExit(1)
    print(f"Executed action #{args.action}: {res['result']}")


def cmd_learn(args: argparse.Namespace) -> None:
    conn = get_connection()
    row = conn.execute("SELECT id FROM agent_tasks WHERE id = ?", (args.task,)).fetchone()
    if not row:
        print("Agent task not found.")
        raise SystemExit(1)
    status = "done" if args.outcome == "success" else "blocked" if args.outcome == "failed" else "learning"
    conn.execute(
        "UPDATE agent_tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
        (status, args.task),
    )
    content = apply_privacy_filters(conn, f"outcome={args.outcome}; notes={args.notes}")
    conn.execute(
        """
        INSERT INTO agent_observations (agent_task_id, observation_type, content, confidence)
        VALUES (?, 'learning', ?, ?)
        """,
        (args.task, content, args.confidence),
    )
    append_event(
        conn,
        "agent_learn",
        "agent_task",
        args.task,
        json.dumps({"outcome": args.outcome, "confidence": args.confidence}, ensure_ascii=True),
    )
    conn.commit()
    print(f"Learned from task #{args.task}: outcome={args.outcome}, status={status}")


def cmd_coach(args: argparse.Namespace) -> None:
    conn = get_connection()
    analogies = _agent_analogies(conn, args.query, limit=args.limit)
    print(f"Assistant coach for: {args.query}")
    if not analogies:
        print("- No strong analogies yet. Delegate or capture more examples first.")
        return
    print("Analogous context:")
    for score, source, content in analogies:
        snippet = content if len(content) <= 160 else content[:157] + "..."
        print(f"- {source} score={score:.3f}: {snippet}")
    print("Suggested playbook:")
    print("- Clarify the outcome and deadline.")
    print("- Check whether there is an owner/dependency risk.")
    print("- Create an approval-gated action before mutating external systems.")
    print("- After completion, run `myos learn` so future coaching improves.")


def cmd_agent_status(args: argparse.Namespace) -> None:
    conn = get_connection()
    if args.task:
        task = conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (args.task,)).fetchone()
        if not task:
            print("Agent task not found.")
            return
        print(f"Agent task #{task['id']}: {task['objective']}")
        print(f"- status={task['status']} priority={task['priority']} updated={task['updated_at']}")
        if task["context"]:
            print(f"- context={task['context']}")
        actions = conn.execute(
            "SELECT id, action_type, title, status, result FROM agent_actions WHERE agent_task_id=? ORDER BY id ASC",
            (args.task,),
        ).fetchall()
        print("Actions:")
        for row in actions:
            suffix = f" result={row['result']}" if row["result"] else ""
            print(f"- #{row['id']} [{row['action_type']}] {row['title']} status={row['status']}{suffix}")
        observations = conn.execute(
            """
            SELECT observation_type, content, confidence
            FROM agent_observations
            WHERE agent_task_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.task, args.limit),
        ).fetchall()
        print("Observations:")
        for row in observations:
            print(f"- [{row['observation_type']}] confidence={row['confidence']:.2f} {row['content']}")
        return

    rows = conn.execute(
        """
        SELECT id, objective, status, priority, updated_at
        FROM agent_tasks
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No agent tasks found.")
        return
    print("Agent tasks:")
    for row in rows:
        title = row["objective"] if len(row["objective"]) <= 100 else row["objective"][:97] + "..."
        print(f"- task #{row['id']} status={row['status']} priority={row['priority']} updated={row['updated_at']} objective={title}")


def _run_autopilot_cycle(args: argparse.Namespace) -> dict[str, int]:
    if args.env_file:
        loaded = load_env_file(args.env_file)
        print(f"Loaded {loaded} vars from {args.env_file}")
    conn = get_connection()
    conn.execute(
        "INSERT INTO autopilot_runs (status, mode) VALUES ('running', ?)",
        (args.mode,),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.commit()

    synced = 0
    watched_files = 0
    watch_suggestions = 0
    signals_detected = 0
    tasks_created = 0
    created_task_ids: list[int] = []
    safe_actions = 0
    approvals_pending = 0
    try:
        if not args.no_sync:
            cmd_sync(argparse.Namespace(connector=args.connector, env_file=""))
            synced = 1
        if not args.no_process:
            cmd_ingest_external(argparse.Namespace(limit=args.external_limit, min_risk=55))
            watched_files, watch_suggestions = _scan_watch_dirs(
                conn,
                limit=args.watch_limit,
                min_confidence=args.min_confidence,
            )
            conn.commit()
            cmd_inbox_process(argparse.Namespace(limit=args.media_limit, min_confidence=args.min_confidence))
            cmd_triage(argparse.Namespace())

        signals = _detect_autopilot_signals(
            conn,
            risk_threshold=args.risk_threshold,
            due_days=args.due_days,
            limit=args.signal_limit,
            watch_risks=getattr(args, "watch_risks", False),
        )
        signals_detected = len(signals)
        for signal in signals:
            task_id = _record_signal_and_task(conn, signal, mode=args.mode, max_actions=args.max_actions)
            if task_id is not None:
                tasks_created += 1
                created_task_ids.append(task_id)
        safe_actions = _execute_safe_autopilot_actions(conn, args.safe_action_limit, created_task_ids)
        approvals_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_actions WHERE status='proposed' AND requires_approval=1"
        ).fetchone()["c"]
        summary = (
            f"synced={synced}, signals={signals_detected}, tasks_created={tasks_created}, "
            f"safe_actions={safe_actions}, approvals_pending={approvals_pending}, "
            f"watched_files={watched_files}, watch_suggestions={watch_suggestions}"
        )
        title, digest_body, digest_payload = _build_autopilot_digest(
            conn,
            run_id=run_id,
            synced=synced,
            signals_detected=signals_detected,
            tasks_created=tasks_created,
            safe_actions=safe_actions,
            approvals_pending=approvals_pending,
            created_task_ids=created_task_ids,
        )
        digest_id = _store_autopilot_digest(conn, title, digest_body, digest_payload, output_dir=args.digest_dir)
        conn.execute(
            """
            UPDATE autopilot_runs
            SET status='completed', finished_at=CURRENT_TIMESTAMP, synced=?, signals_detected=?,
                tasks_created=?, safe_actions_executed=?, approvals_pending=?, summary=?
            WHERE id=?
            """,
            (synced, signals_detected, tasks_created, safe_actions, approvals_pending, summary, run_id),
        )
        append_event(conn, "autopilot_cycle", "autopilot_run", run_id, json.dumps({"summary": summary}, ensure_ascii=True))
        conn.commit()
        # Context Intelligence Loop runs AFTER the cycle's atomic commit (review #5): reflect()
        # /hygiene() commit internally, so doing this mid-cycle would defeat the cycle's
        # rollback boundary. As a separate committed post-step it can't corrupt cycle state.
        try:
            reflection = ctx.reflect(conn)
            hygiene_stats = ctx.hygiene(conn)
            append_event(
                conn, "context_reflect", "autopilot_run", run_id,
                json.dumps({**reflection, **hygiene_stats}, ensure_ascii=True),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 — never fail an autopilot cycle on the reflection step
            # Roll back any partial reflect/hygiene write so it can't be flushed by the
            # subsequent _notify_digest commit (review #4, defensive — the cycle already
            # committed at line 3301 and reflect/hygiene self-commit).
            conn.rollback()
        _notify_digest(conn, digest_id, title, digest_body, digest_payload)
        conn.commit()
        print(f"Autopilot cycle complete (run_id={run_id}, digest_id={digest_id}): {summary}")
        if approvals_pending:
            print("Needs your approval:")
            pending = conn.execute(
                """
                SELECT id, agent_task_id, action_type, title
                FROM agent_actions
                WHERE status='proposed' AND requires_approval=1
                ORDER BY created_at ASC
                LIMIT 10
                """
            ).fetchall()
            for row in pending:
                print(f"- action #{row['id']} task=#{row['agent_task_id']} [{row['action_type']}] {row['title']}")
            print("Run: myos approve --list")
        return {
            "run_id": run_id,
            "synced": synced,
            "signals_detected": signals_detected,
            "tasks_created": tasks_created,
            "safe_actions": safe_actions,
            "approvals_pending": approvals_pending,
        }
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE autopilot_runs SET status='failed', finished_at=CURRENT_TIMESTAMP, summary=? WHERE id=?",
            (str(exc), run_id),
        )
        conn.commit()
        raise


def cmd_autopilot(args: argparse.Namespace) -> None:
    lock_conn = get_connection()
    owner = f"autopilot-{os.getpid()}"
    cycles = 0
    while True:
        # Per-cycle mutual exclusion: two `myos autopilot` processes can't run
        # overlapping cycles (which would corrupt shared state). The lock is held
        # only for the cycle's duration, then released, so it never goes stale.
        if acquire_lock(lock_conn, "autopilot", owner):
            try:
                _run_autopilot_cycle(args)
            finally:
                release_lock(lock_conn, "autopilot", owner)
                lock_conn.commit()
        else:
            print("autopilot: another instance is mid-cycle; skipping this tick.")
        cycles += 1
        if args.once or (args.max_cycles and cycles >= args.max_cycles):
            return
        time.sleep(args.interval_sec)


def cmd_approve(args: argparse.Namespace) -> None:
    conn = get_connection()
    if args.list:
        rows = conn.execute(
            """
            SELECT id, agent_task_id, action_type, title, status, payload_json
            FROM agent_actions
            WHERE requires_approval=1 AND status IN ('proposed', 'approved')
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            print("No approval-needed actions.")
            return
        print("Approval queue:")
        for row in rows:
            print(f"- action #{row['id']} task=#{row['agent_task_id']} [{row['action_type']}] {row['title']} status={row['status']}")
            payload = json.loads(row["payload_json"] or "{}")
            print(f"  target: {_provider_target_summary(payload)}")
            preview = payload.get("draft") or payload.get("text")
            if preview:
                snippet = str(preview) if len(str(preview)) <= 220 else str(preview)[:217] + "..."
                print(f"  preview: {snippet}")
        return
    if args.action is None:
        print("Provide --action ID or use --list.")
        raise SystemExit(1)
    cmd_act(argparse.Namespace(task=None, action=args.action, list=False, approve=True, execute=args.execute, limit=args.limit))


def cmd_autopilot_status(args: argparse.Namespace) -> None:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, status, started_at, finished_at, summary
        FROM autopilot_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No autopilot runs found.")
    else:
        print("Autopilot runs:")
        for row in rows:
            print(
                f"- run #{row['id']} status={row['status']} started={row['started_at']} "
                f"finished={row['finished_at'] or 'running'} summary={row['summary'] or ''}"
            )
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM agent_actions WHERE requires_approval=1 AND status='proposed'"
    ).fetchone()["c"]
    open_tasks = conn.execute("SELECT COUNT(*) AS c FROM agent_tasks WHERE status='open'").fetchone()["c"]
    print(f"Autopilot state: open_agent_tasks={open_tasks} approvals_pending={pending}")


def cmd_digest(args: argparse.Namespace) -> None:
    conn = get_connection()
    row = None
    if args.id:
        row = conn.execute(
            "SELECT id, title, body, created_at FROM assistant_digests WHERE id = ?",
            (args.id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id, title, body, created_at
            FROM assistant_digests
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        print("No assistant digest found. Run `myos autopilot --once` first.")
        return
    if args.title_only:
        print(f"Digest #{row['id']}: {row['title']} ({row['created_at']})")
        return
    print(row["body"].rstrip())


def cmd_goal(args: argparse.Namespace) -> None:
    conn = get_connection()
    if args.goal_action == "add":
        objective = apply_privacy_filters(conn, args.objective)
        context = apply_privacy_filters(conn, args.context)
        conn.execute(
            """
            INSERT INTO assistant_goals (objective, context, cadence_minutes, priority, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (objective, context, args.cadence_minutes, args.priority),
        )
        goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
        print(f"Added assistant goal #{goal_id}: {objective}")
        return
    if args.goal_action == "pause":
        conn.execute(
            "UPDATE assistant_goals SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (args.id,),
        )
        conn.commit()
        print(f"Paused assistant goal #{args.id}.")
        return
    if args.goal_action == "resume":
        conn.execute(
            "UPDATE assistant_goals SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (args.id,),
        )
        conn.commit()
        print(f"Resumed assistant goal #{args.id}.")
        return
    rows = conn.execute(
        """
        SELECT id, objective, status, cadence_minutes, priority, last_evaluated_at
        FROM assistant_goals
        ORDER BY status ASC, priority ASC, id ASC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No assistant goals found.")
        return
    print("Assistant goals:")
    for row in rows:
        print(
            f"- goal #{row['id']} status={row['status']} priority={row['priority']} "
            f"cadence={row['cadence_minutes']}m last={row['last_evaluated_at'] or 'never'} objective={row['objective']}"
        )


def cmd_self_review(_: argparse.Namespace) -> None:
    conn = get_connection()
    policy = get_policy_map(conn)
    checks: list[tuple[str, bool, str]] = []
    active_goals = conn.execute("SELECT COUNT(*) AS c FROM assistant_goals WHERE status='active'").fetchone()["c"]
    recent_runs = conn.execute(
        "SELECT COUNT(*) AS c FROM autopilot_runs WHERE started_at >= datetime('now', '-1 day')"
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM agent_actions WHERE requires_approval=1 AND status='proposed'"
    ).fetchone()["c"]
    action_provider = bool(os.getenv("MYOS_ACTION_COMMAND", "").strip())
    ai_provider = bool(os.getenv("MYOS_AI_COMMAND", "").strip())
    connectors_ready = conn.execute("SELECT COUNT(*) AS c FROM sync_state WHERE last_status='ok'").fetchone()["c"]

    checks.append(("standing_goals", active_goals > 0, f"active_goals={active_goals}"))
    checks.append(("autopilot_recent", recent_runs > 0, f"runs_24h={recent_runs}"))
    checks.append(("approval_queue", pending < 20, f"pending_approvals={pending}"))
    checks.append(("ai_reasoning", ai_provider or policy.get("ai_provider") == "local", f"ai_command={'yes' if ai_provider else 'no'}"))
    checks.append(("action_provider", action_provider, f"action_command={'yes' if action_provider else 'no'}"))
    checks.append(("live_connectors", connectors_ready > 0, f"connectors_ok={connectors_ready}"))

    missing = [name for name, ok, _ in checks if not ok]
    status = "ready" if not missing else "needs_setup"
    summary = ", ".join(f"{name}={'ok' if ok else 'missing'}" for name, ok, _ in checks)
    conn.execute(
        """
        INSERT INTO assistant_self_reviews (status, summary, missing_capabilities_json)
        VALUES (?, ?, ?)
        """,
        (status, summary, json.dumps(missing, ensure_ascii=True)),
    )
    conn.commit()
    print(f"Autonomy self-review: {status}")
    for name, ok, detail in checks:
        print(f"- {'PASS' if ok else 'GAP'} {name}: {detail}")
    if missing:
        print("Next setup gaps:")
        for item in missing:
            print(f"- {item}")


def cmd_watch_dir(args: argparse.Namespace) -> None:
    conn = get_connection()
    if args.watch_action == "add":
        path = str(Path(args.path).expanduser())
        conn.execute(
            """
            INSERT INTO assistant_watch_dirs (path, label, status, updated_at)
            VALUES (?, ?, 'active', CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET label=excluded.label, status='active', updated_at=CURRENT_TIMESTAMP
            """,
            (path, args.label),
        )
        conn.commit()
        print(f"Watching directory: {path}")
        return
    if args.watch_action == "pause":
        conn.execute(
            "UPDATE assistant_watch_dirs SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (args.id,),
        )
        conn.commit()
        print(f"Paused watch directory #{args.id}.")
        return
    if args.watch_action == "resume":
        conn.execute(
            "UPDATE assistant_watch_dirs SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (args.id,),
        )
        conn.commit()
        print(f"Resumed watch directory #{args.id}.")
        return
    rows = conn.execute(
        """
        SELECT id, path, label, status
        FROM assistant_watch_dirs
        ORDER BY id ASC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No watch directories configured.")
        return
    print("Watch directories:")
    for row in rows:
        label = f" label={row['label']}" if row["label"] else ""
        print(f"- #{row['id']} status={row['status']}{label} path={row['path']}")


def cmd_watch_scan(args: argparse.Namespace) -> None:
    conn = get_connection()
    files, suggestions = _scan_watch_dirs(conn, limit=args.limit, min_confidence=args.min_confidence)
    conn.commit()
    print(f"Watch scan complete: files_ingested={files}, suggestions_created={suggestions}")


def cmd_pulse(args: argparse.Namespace) -> None:
    if args.env_file:
        load_env_file(args.env_file)
    if args.once:
        outputs = run_cycle(meeting_hours=args.meeting_hours)
        print("Pulse cycle done:", ", ".join(outputs))
        return
    lock_conn = get_connection()
    owner = f"pulse-{os.getpid()}"
    while True:
        if acquire_lock(lock_conn, "pulse", owner):
            try:
                outputs = run_cycle(meeting_hours=args.meeting_hours)
                print(f"[{datetime.now().isoformat(timespec='seconds')}] cycle -> {', '.join(outputs)}")
            finally:
                release_lock(lock_conn, "pulse", owner)
                lock_conn.commit()
        else:
            print("pulse: another instance is mid-cycle; skipping this tick.")
        time.sleep(args.interval_sec)


def cmd_chat(args: argparse.Namespace) -> None:
    if getattr(args, "env_file", ""):
        load_env_file(args.env_file)
    conn = get_connection()
    backend = providers.get_backend(args.backend or None)
    ok, detail = backend.available()
    if not ok:
        print(f"Backend '{backend.name}' is not available: {detail}")
        raise SystemExit(1)
    print(f"MYOS chat [{backend.name}] — ask anything; external changes are proposed for your approval.")
    print("Type 'exit' to quit.")
    history: list[dict] = []
    conversation_id: int | None = None
    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in ("exit", "quit", ":q"):
            break
        result = assistant.run_turn(
            conn, user, history, backend_name=args.backend or None,
            surface="chat", conversation_id=conversation_id,
        )
        conversation_id = result.get("conversation_id", conversation_id)
        history = result.get("history", history)
        reply = (result.get("reply") or "").strip()
        if reply:
            print(f"\nmyos> {reply}")
        _handle_proposals(conn, result.get("proposed_action_ids", []))


def cmd_voice(args: argparse.Namespace) -> None:
    from . import voice

    if getattr(args, "env_file", ""):
        load_env_file(args.env_file)
    conn = get_connection()
    backend = providers.get_backend(args.backend or None)
    ok, detail = backend.available()
    if not ok:
        print(f"Backend '{backend.name}' is not available: {detail}")
        raise SystemExit(1)
    print(f"MYOS voice [{backend.name}] — push-to-talk. Ctrl-C to quit.")
    history: list[dict] = []
    conversation_id: int | None = None
    while True:
        try:
            wav = voice.record_push_to_talk()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not wav:
            print("Voice capture unavailable; exiting voice mode.")
            break
        text = voice.transcribe(wav)
        try:
            os.remove(wav)
        except OSError:
            pass
        if not text:
            print("(heard nothing — try again)")
            continue
        print(f"you> {text}")
        result = assistant.run_turn(
            conn, text, history, backend_name=args.backend or None,
            surface="voice", conversation_id=conversation_id,
        )
        conversation_id = result.get("conversation_id", conversation_id)
        history = result.get("history", history)
        reply = (result.get("reply") or "").strip()
        if reply:
            print(f"myos> {reply}")
            if not args.text_reply:
                voice.speak(reply)
        _handle_proposals(conn, result.get("proposed_action_ids", []))


def cmd_team(args: argparse.Namespace) -> None:
    conn = get_connection()
    if getattr(args, "team_action", None) == "add":
        pid = em.upsert_person(conn, args.name, role=args.role, team=args.team, relation=args.relation)
        conn.commit()
        print(f"Saved person #{pid}: {args.name}")
        return
    rows = em.list_team(conn)
    if not rows:
        print("No people tracked yet. Add one: myos team add \"<name>\" --role ... --relation report")
        return
    print("Team & stakeholders:")
    for r in rows:
        extra = "".join(filter(None, [f" — {r['role']}" if r["role"] else "", f" @{r['team']}" if r["team"] else ""]))
        print(f"- {r['name']} ({r['relation']}){extra}")


def cmd_note(args: argparse.Namespace) -> None:
    conn = get_connection()
    res = em.route_note(conn, args.text)
    conn.commit()
    routed = res.pop("routed", "inbox")
    detail = ", ".join(f"{k}={v}" for k, v in res.items() if k not in ("created",))
    print(f"Inferred and routed → {routed}" + (f" ({detail})" if detail else ""))


def cmd_one_on_one(args: argparse.Namespace) -> None:
    conn = get_connection()
    res = em.log_one_on_one(conn, args.person, args.notes)
    conn.commit()
    print(f"Logged 1:1 #{res['one_on_one_id']} with {args.person}; "
          f"{len(res['action_item_ids'])} action item(s) captured to your inbox.")


def cmd_meeting(args: argparse.Namespace) -> None:
    conn = get_connection()
    text = args.text or ""
    source = "manual"
    if args.audio:
        from . import voice
        text = voice.transcribe(args.audio) or text
        source = "audio"
        if not text:
            print("No transcript produced (install faster-whisper, or pass notes as text).")
            return
    title = args.title or em._first_sentence(text, 60) or "Meeting"
    res = em.capture_meeting(conn, title, text, source=source)
    conn.commit()
    print(f"Captured meeting #{res['meeting_id']} '{title}': "
          f"{res['action_items']} action item(s), {len(res['item_ids'])} item(s) total.")


def cmd_review_draft(args: argparse.Namespace) -> None:
    conn = get_connection()
    print(em.build_review_packet(conn, args.person))


def cmd_risk_scan(args: argparse.Namespace) -> None:
    conn = get_connection()
    findings = watch.scan_project_risks(conn, risk_threshold=args.risk_threshold, limit=args.limit)
    if not findings:
        print("No project risks detected. (Sync connectors first: myos sync --connector all)")
        return
    print(f"Project risks ({len(findings)}):")
    for f in findings:
        owner = f" — {f['owner']}" if f["owner"] else ""
        print(f"- [{f['severity']}] {f['kind']}: {f['title']} ({f['reason']}){owner}")
    if args.draft_nudges:
        ids = watch.draft_nudges(conn, findings, limit=args.nudge_limit)
        print(f"\nDrafted {len(ids)} nudge(s) for approval: {', '.join('#' + str(i) for i in ids)}")
        print("Review and send (graded autonomy gates external posts): myos approve --list")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myos",
        description="Local-first personal assistant OS (CLI).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Capture an inbox item.")
    capture.add_argument("text", help="Raw capture text.")
    capture.add_argument("--kind", choices=["note", "task", "commitment", "decision", "risk"])
    capture.add_argument("--due", help="Due date in YYYY-MM-DD format.")
    capture.add_argument("--owner", help="Owner name.")
    capture.set_defaults(func=cmd_capture)

    triage = sub.add_parser("triage", help="Triage inbox into work items.")
    triage.set_defaults(func=cmd_triage)

    today = sub.add_parser("today", help="Generate today's focus list.")
    today.add_argument("--meeting-hours", type=float, default=0.0)
    today.set_defaults(func=cmd_today)

    risk = sub.add_parser("risk-radar", help="Show current risk-ranked items.")
    risk.set_defaults(func=cmd_risk_radar)

    close = sub.add_parser("close-day", help="Close day and write summary log.")
    close.add_argument("--mode", choices=["maker", "hybrid", "meeting-heavy", "recovery"], default="hybrid")
    close.add_argument("--note", default="")
    close.set_defaults(func=cmd_close_day)

    transcribe = sub.add_parser("transcribe", help="Transcribe an audio file into indexed context.")
    transcribe.add_argument("audio_file", help="Path to audio file.")
    transcribe.add_argument("--text", default="", help="Optional manual transcript text.")
    transcribe.set_defaults(func=cmd_transcribe)

    image = sub.add_parser("ingest-image", help="OCR an image into indexed context.")
    image.add_argument("image_file", help="Path to image file.")
    image.add_argument("--text", default="", help="Optional manual extracted text.")
    image.set_defaults(func=cmd_ingest_image)

    link = sub.add_parser("link", help="Link two work items in the knowledge graph.")
    link.add_argument("--from-item", type=int, required=True)
    link.add_argument("--to-item", type=int, required=True)
    link.add_argument("--relation", default="relates_to")
    link.add_argument("--weight", type=float, default=1.0)
    link.set_defaults(func=cmd_link)

    related = sub.add_parser("related", help="Show graph-related work items.")
    related.add_argument("--item", type=int, required=True)
    related.add_argument("--limit", type=int, default=10)
    related.set_defaults(func=cmd_related)

    context = sub.add_parser("context", help="Find semantic context from indexed chunks.")
    context.add_argument("query", help="Search query.")
    context.add_argument("--limit", type=int, default=5)
    context.set_defaults(func=cmd_context)

    recall = sub.add_parser("recall", help="Scored recall over conversation memory (relevance+recency+importance).")
    recall.add_argument("query", help="What to recall.")
    recall.add_argument("--limit", type=int, default=5)
    recall.set_defaults(func=cmd_recall)

    reflect = sub.add_parser("reflect", help="Distill observations into insights + relationships; run memory hygiene.")
    reflect.set_defaults(func=cmd_reflect)

    suggestions = sub.add_parser("suggestions", help="List/accept/dismiss tracked improvement suggestions.")
    suggestions.add_argument("suggestions_action", nargs="?", choices=["list", "accept", "dismiss", "apply"], default="list")
    suggestions.add_argument("id", nargs="?", type=int)
    suggestions.add_argument("--status", default="proposed")
    suggestions.add_argument("--feedback", default="")
    suggestions.set_defaults(func=cmd_suggestions)

    memory = sub.add_parser("memory", help="Overview of logged conversations, observations, insights, relationships.")
    memory.set_defaults(func=cmd_memory)

    reindex = sub.add_parser("reindex", help="Backfill graph nodes and chunks for existing data.")
    reindex.set_defaults(func=cmd_reindex)

    sync = sub.add_parser("sync", help="Sync external connectors.")
    sync.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    sync.add_argument("--env-file", default="")
    sync.set_defaults(func=cmd_sync)

    config_init = sub.add_parser("config-init", help="Create local env template for connector credentials.")
    config_init.add_argument("--path", default="./.env.myos")
    config_init.add_argument("--force", action="store_true")
    config_init.set_defaults(func=cmd_config_init)

    setup_live = sub.add_parser("setup-live", help="Prepare live Autopilot config, folders, goals, and safe defaults.")
    setup_live.add_argument("--apply", action="store_true")
    setup_live.add_argument("--check", action="store_true")
    setup_live.add_argument("--force", action="store_true")
    setup_live.add_argument("--data-dir", default="")
    setup_live.add_argument("--env-file", default="")
    setup_live.add_argument("--db-path", default="")
    setup_live.add_argument("--watch-dir", default="")
    setup_live.add_argument("--install-launchd", action="store_true")
    setup_live.add_argument("--load-launchd", action="store_true")
    setup_live.add_argument("--autopilot-interval-sec", type=int, default=900)
    setup_live.set_defaults(func=cmd_setup_live)

    onboard = sub.add_parser("onboard", help="Show connector onboarding diagnostics.")
    onboard.set_defaults(func=cmd_onboard)

    doctor = sub.add_parser("doctor", help="Show local system and connector health.")
    doctor.set_defaults(func=cmd_doctor)

    ingest_external = sub.add_parser("ingest-external", help="Ingest synced external items into inbox.")
    ingest_external.add_argument("--limit", type=int, default=100)
    ingest_external.add_argument("--min-risk", type=int, default=55)
    ingest_external.set_defaults(func=cmd_ingest_external)

    process = sub.add_parser("inbox-process", help="Extract suggested inbox items from media assets.")
    process.add_argument("--limit", type=int, default=20)
    process.add_argument("--min-confidence", type=float, default=0.65)
    process.set_defaults(func=cmd_inbox_process)

    why = sub.add_parser("why", help="Explain why a work item exists.")
    why.add_argument("--item", type=int, required=True)
    why.set_defaults(func=cmd_why)

    at_risk = sub.add_parser("at-risk", help="Show at-risk work items.")
    at_risk.add_argument("--threshold", type=int, default=60)
    at_risk.add_argument("--limit", type=int, default=10)
    at_risk.set_defaults(func=cmd_at_risk)

    waiting = sub.add_parser("waiting-on", help="Show waiting-on items with owners.")
    waiting.add_argument("--limit", type=int, default=10)
    waiting.set_defaults(func=cmd_waiting_on)

    delegate = sub.add_parser("delegation-candidates", help="Show likely delegation candidates.")
    delegate.add_argument("--limit", type=int, default=10)
    delegate.set_defaults(func=cmd_delegation_candidates)

    brief = sub.add_parser("brief", help="Generate executive daily brief.")
    brief.add_argument("--meeting-hours", type=float, default=0.0)
    brief.add_argument("--top", type=int, default=10)
    brief.add_argument("--risk-threshold", type=int, default=60)
    brief.set_defaults(func=cmd_brief)

    stop_doing = sub.add_parser("stop-doing", help="Suggest what to defer/delegate/drop.")
    stop_doing.add_argument("--capacity", type=int, default=8)
    stop_doing.add_argument("--deep-budget", type=int, default=3)
    stop_doing.add_argument("--keep-risk", type=int, default=60)
    stop_doing.add_argument("--limit", type=int, default=10)
    stop_doing.set_defaults(func=cmd_stop_doing)

    report = sub.add_parser("report", help="Generate markdown daily report.")
    report.add_argument("--meeting-hours", type=float, default=0.0)
    report.add_argument("--risk-threshold", type=int, default=60)
    report.add_argument("--output-dir", default="")
    report.set_defaults(func=cmd_report)

    run_day = sub.add_parser("run-day", help="Run autonomous daily pipeline end-to-end.")
    run_day.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    run_day.add_argument("--env-file", default="")
    run_day.add_argument("--meeting-hours", type=float, default=0.0)
    run_day.add_argument("--external-limit", type=int, default=100)
    run_day.add_argument("--media-limit", type=int, default=30)
    run_day.add_argument("--min-confidence", type=float, default=0.65)
    run_day.add_argument("--risk-threshold", type=int, default=60)
    run_day.add_argument("--capacity", type=int, default=8)
    run_day.add_argument("--deep-budget", type=int, default=3)
    run_day.add_argument("--keep-risk", type=int, default=60)
    run_day.add_argument("--stop-limit", type=int, default=10)
    run_day.add_argument("--output-dir", default="")
    run_day.set_defaults(func=cmd_run_day)

    go_live = sub.add_parser("go-live", help="Validate live connectors and run first live ingestion + triage.")
    go_live.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    go_live.add_argument("--env-file", default="")
    go_live.add_argument("--external-limit", type=int, default=100)
    go_live.set_defaults(func=cmd_go_live)

    metrics = sub.add_parser("metrics", help="Show KPI snapshot for assistant health.")
    metrics.add_argument("--days", type=int, default=7)
    metrics.add_argument("--risk-threshold", type=int, default=60)
    metrics.set_defaults(func=cmd_metrics)

    log_evidence = sub.add_parser("log-evidence", help="Log performance/review evidence.")
    log_evidence.add_argument("--person", required=True)
    log_evidence.add_argument("--category", required=True)
    log_evidence.add_argument("--impact", required=True)
    log_evidence.add_argument("--artifact-link", default="")
    log_evidence.add_argument("--privacy", choices=["internal", "confidential", "restricted"], default="internal")
    log_evidence.set_defaults(func=cmd_log_evidence)

    review_evidence = sub.add_parser("review-evidence", help="List review evidence entries.")
    review_evidence.add_argument("--person", default="")
    review_evidence.add_argument("--limit", type=int, default=20)
    review_evidence.set_defaults(func=cmd_review_evidence)

    resolve_commitment = sub.add_parser("resolve-commitment", help="Resolve commitment outcome for a work item.")
    resolve_commitment.add_argument("--item", type=int, required=True)
    resolve_commitment.add_argument(
        "--outcome",
        choices=["auto", "completed_on_time", "completed_late", "missed"],
        default="auto",
    )
    resolve_commitment.add_argument("--resolved-on", default="")
    resolve_commitment.add_argument("--notes", default="")
    resolve_commitment.set_defaults(func=cmd_resolve_commitment)

    weekly = sub.add_parser("weekly-review", help="Generate weekly review health summary.")
    weekly.add_argument("--days", type=int, default=7)
    weekly.add_argument("--risk-threshold", type=int, default=60)
    weekly.add_argument("--risk-alert", type=int, default=5)
    weekly.set_defaults(func=cmd_weekly_review)

    launchd_install = sub.add_parser("launchd-install", help="Install launchd agents for sync/pulse.")
    launchd_install.add_argument("--apply", action="store_true")
    launchd_install.add_argument("--load", action="store_true")
    launchd_install.add_argument("--env-file", default="")
    launchd_install.add_argument("--interval-sec", type=int, default=1800)
    launchd_install.add_argument("--meeting-hours", type=float, default=0.0)
    launchd_install.add_argument("--autopilot", action="store_true")
    launchd_install.add_argument("--autopilot-interval-sec", type=int, default=900)
    launchd_install.set_defaults(func=cmd_launchd_install)

    launchd_uninstall = sub.add_parser("launchd-uninstall", help="Remove launchd agents for sync/pulse.")
    launchd_uninstall.add_argument("--apply", action="store_true")
    launchd_uninstall.set_defaults(func=cmd_launchd_uninstall)

    activate = sub.add_parser("activate", help="Run end-to-end activation flow.")
    activate.add_argument("--env-file", default="")
    activate.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    activate.add_argument("--external-limit", type=int, default=100)
    activate.add_argument("--install-launchd", action="store_true")
    activate.add_argument("--load-launchd", action="store_true")
    activate.set_defaults(func=cmd_activate)

    launchd_status = sub.add_parser("launchd-status", help="Show whether MYOS launch agents are loaded.")
    launchd_status.set_defaults(func=cmd_launchd_status)

    start = sub.add_parser("start", help="Start MYOS runtime and run sanity checks.")
    start.add_argument("--env-file", default="")
    start.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    start.add_argument("--external-limit", type=int, default=100)
    start.add_argument("--report-dir", default="")
    start.add_argument("--install-launchd", action="store_true")
    start.add_argument("--load-launchd", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="Stop MYOS runtime launch agents.")
    stop.set_defaults(func=cmd_stop)

    dashboard = sub.add_parser("dashboard", help="Serve or export local dashboard.")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8787)
    dashboard.add_argument("--report-dir", default="")
    dashboard.add_argument("--once", action="store_true")
    dashboard.add_argument("--output-html", default="")
    dashboard.set_defaults(func=cmd_dashboard)

    sanity = sub.add_parser("sanity", help="Run operational sanity checks.")
    sanity.add_argument("--strict", action="store_true")
    sanity.add_argument("--report-dir", default="")
    sanity.set_defaults(func=cmd_sanity)

    runbook = sub.add_parser("runbook", help="Print daily/weekly operational runbook.")
    runbook.add_argument("--short", action="store_true")
    runbook.set_defaults(func=cmd_runbook)

    cleanup = sub.add_parser("cleanup", help="Archive stale open work items.")
    cleanup.add_argument("--days", type=int, default=30)
    cleanup.add_argument("--limit", type=int, default=100)
    cleanup.set_defaults(func=cmd_cleanup)

    renegotiate = sub.add_parser("renegotiate", help="Show at-risk commitments needing renegotiation.")
    renegotiate.add_argument("--days-ahead", type=int, default=2)
    renegotiate.add_argument("--default-extension-days", type=int, default=3)
    renegotiate.add_argument("--limit", type=int, default=20)
    renegotiate.set_defaults(func=cmd_renegotiate)

    next_action = sub.add_parser("next-action", help="Recommend one highest-value next action.")
    next_action.add_argument("--meeting-hours", type=float, default=0.0)
    next_action.add_argument("--risk-threshold", type=int, default=60)
    next_action.set_defaults(func=cmd_next_action)

    snapshot = sub.add_parser("snapshot", help="Export machine-readable state snapshot as JSON.")
    snapshot.add_argument("--risk-threshold", type=int, default=60)
    snapshot.add_argument("--limit", type=int, default=10)
    snapshot.add_argument("--output", default="")
    snapshot.set_defaults(func=cmd_snapshot)

    orchestrate = sub.add_parser("orchestrate", help="Run a tracked workflow orchestration.")
    orchestrate.add_argument("--workflow", choices=["daily", "weekly", "incident"], required=True)
    orchestrate.add_argument("--env-file", default="")
    orchestrate.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    orchestrate.add_argument("--meeting-hours", type=float, default=0.0)
    orchestrate.add_argument("--external-limit", type=int, default=100)
    orchestrate.add_argument("--media-limit", type=int, default=30)
    orchestrate.add_argument("--min-confidence", type=float, default=0.65)
    orchestrate.add_argument("--risk-threshold", type=int, default=60)
    orchestrate.add_argument("--capacity", type=int, default=8)
    orchestrate.add_argument("--deep-budget", type=int, default=3)
    orchestrate.add_argument("--keep-risk", type=int, default=60)
    orchestrate.add_argument("--stop-limit", type=int, default=10)
    orchestrate.add_argument("--output-dir", default="")
    orchestrate.set_defaults(func=cmd_orchestrate)

    workflow_runs = sub.add_parser("workflow-runs", help="List tracked workflow runs.")
    workflow_runs.add_argument("--limit", type=int, default=20)
    workflow_runs.set_defaults(func=cmd_workflow_runs)

    policy = sub.add_parser("policy", help="View or set privacy/retention policy.")
    policy.add_argument("--set", default="", help="Set one policy value (KEY=VALUE).")
    policy.set_defaults(func=cmd_policy)

    queue_add = sub.add_parser("queue-add", help="Queue a workflow run for worker processing.")
    queue_add.add_argument("--workflow", choices=["daily", "weekly", "incident"], required=True)
    queue_add.add_argument("--payload", default="", help="Optional JSON payload of workflow args.")
    queue_add.set_defaults(func=cmd_queue_add)

    worker = sub.add_parser("worker", help="Process queued workflow jobs.")
    worker.add_argument("--limit", type=int, default=5)
    worker.set_defaults(func=cmd_worker)

    cutover_check = sub.add_parser("cutover-check", help="Check live credential/sync readiness before cutover.")
    cutover_check.set_defaults(func=cmd_cutover_check)

    uat = sub.add_parser("uat", help="Evaluate UAT quality metrics on recent data.")
    uat.add_argument("--days", type=int, default=7)
    uat.add_argument("--risk-threshold", type=int, default=60)
    uat.add_argument("--min-sample", type=int, default=5)
    uat.add_argument("--backlog-warn", type=int, default=15)
    uat.add_argument("--acceptance-warn", type=float, default=60.0)
    uat.add_argument("--risk-focus-warn", type=float, default=20.0)
    uat.set_defaults(func=cmd_uat)

    tune = sub.add_parser("tune", help="Suggest UAT thresholds from recent operating data.")
    tune.add_argument("--days", type=int, default=14)
    tune.add_argument("--apply-policy", action="store_true")
    tune.set_defaults(func=cmd_tune)

    delegate = sub.add_parser("delegate", help="Delegate an objective to the autonomous assistant core.")
    delegate.add_argument("objective", help="Outcome or task objective for the assistant.")
    delegate.add_argument("--context", default="", help="Additional context, transcript snippet, or constraints.")
    delegate.add_argument("--constraint", action="append", default=[], help="Repeatable constraint for this task.")
    delegate.add_argument("--mode", choices=["safe", "balanced", "aggressive"], default="safe")
    delegate.add_argument("--priority", type=int, default=2)
    delegate.add_argument("--max-actions", type=int, default=5)
    delegate.add_argument("--analogy-limit", type=int, default=5)
    delegate.add_argument("--to", default="", help="Harness an external agent CLI (copilot|cursor|claude) to execute this objective.")
    delegate.set_defaults(func=cmd_delegate)

    act = sub.add_parser("act", help="List, approve, and execute assistant-proposed actions.")
    act.add_argument("--task", type=int)
    act.add_argument("--action", type=int)
    act.add_argument("--list", action="store_true")
    act.add_argument("--approve", action="store_true")
    act.add_argument("--execute", action="store_true")
    act.add_argument("--limit", type=int, default=20)
    act.set_defaults(func=cmd_act)

    learn = sub.add_parser("learn", help="Teach the assistant the outcome of a delegated task.")
    learn.add_argument("--task", type=int, required=True)
    learn.add_argument("--outcome", choices=["success", "partial", "failed"], required=True)
    learn.add_argument("--notes", default="")
    learn.add_argument("--confidence", type=float, default=0.8)
    learn.set_defaults(func=cmd_learn)

    coach = sub.add_parser("coach", help="Get analogy-based coaching from assistant memory.")
    coach.add_argument("query", help="Situation or decision you want help with.")
    coach.add_argument("--limit", type=int, default=5)
    coach.set_defaults(func=cmd_coach)

    agent_status = sub.add_parser("agent-status", help="Show assistant tasks, actions, and observations.")
    agent_status.add_argument("--task", type=int)
    agent_status.add_argument("--limit", type=int, default=20)
    agent_status.set_defaults(func=cmd_agent_status)

    autopilot = sub.add_parser("autopilot", help="Run the always-on intelligent assistant loop.")
    autopilot.add_argument("--env-file", default="")
    autopilot.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    autopilot.add_argument("--once", action="store_true")
    autopilot.add_argument("--interval-sec", type=int, default=900)
    autopilot.add_argument("--max-cycles", type=int, default=0)
    autopilot.add_argument("--mode", choices=["safe", "balanced", "aggressive"], default="safe")
    autopilot.add_argument("--risk-threshold", type=int, default=60)
    autopilot.add_argument("--due-days", type=int, default=2)
    autopilot.add_argument("--signal-limit", type=int, default=10)
    autopilot.add_argument("--max-actions", type=int, default=5)
    autopilot.add_argument("--safe-action-limit", type=int, default=20)
    autopilot.add_argument("--external-limit", type=int, default=100)
    autopilot.add_argument("--media-limit", type=int, default=30)
    autopilot.add_argument("--min-confidence", type=float, default=0.65)
    autopilot.add_argument("--watch-limit", type=int, default=20)
    autopilot.add_argument("--digest-dir", default="")
    autopilot.add_argument("--no-sync", action="store_true")
    autopilot.add_argument("--no-process", action="store_true")
    autopilot.add_argument("--watch-risks", action="store_true", help="Proactively detect project risks each cycle and draft nudges (approval-gated).")
    autopilot.set_defaults(func=cmd_autopilot)

    approve = sub.add_parser("approve", help="Review, approve, and optionally execute autopilot actions.")
    approve.add_argument("--list", action="store_true")
    approve.add_argument("--action", type=int)
    approve.add_argument("--execute", action="store_true")
    approve.add_argument("--limit", type=int, default=20)
    approve.set_defaults(func=cmd_approve)

    autopilot_status = sub.add_parser("autopilot-status", help="Show autopilot runs and pending approvals.")
    autopilot_status.add_argument("--limit", type=int, default=10)
    autopilot_status.set_defaults(func=cmd_autopilot_status)

    digest = sub.add_parser("digest", help="Show latest assistant digest.")
    digest.add_argument("--id", type=int, default=0)
    digest.add_argument("--title-only", action="store_true")
    digest.set_defaults(func=cmd_digest)

    goal = sub.add_parser("goal", help="Manage standing goals that autopilot evaluates automatically.")
    goal_sub = goal.add_subparsers(dest="goal_action", required=True)
    goal_add = goal_sub.add_parser("add", help="Add a standing assistant goal.")
    goal_add.add_argument("objective")
    goal_add.add_argument("--context", default="")
    goal_add.add_argument("--cadence-minutes", type=int, default=1440)
    goal_add.add_argument("--priority", type=int, default=2)
    goal_add.set_defaults(func=cmd_goal)
    goal_list = goal_sub.add_parser("list", help="List assistant goals.")
    goal_list.add_argument("--limit", type=int, default=50)
    goal_list.set_defaults(func=cmd_goal)
    goal_pause = goal_sub.add_parser("pause", help="Pause a standing goal.")
    goal_pause.add_argument("--id", type=int, required=True)
    goal_pause.set_defaults(func=cmd_goal)
    goal_resume = goal_sub.add_parser("resume", help="Resume a standing goal.")
    goal_resume.add_argument("--id", type=int, required=True)
    goal_resume.set_defaults(func=cmd_goal)

    self_review = sub.add_parser("self-review", help="Review whether the assistant is truly autonomous yet.")
    self_review.set_defaults(func=cmd_self_review)

    action_provider = sub.add_parser("action-provider", help="Built-in approved-action provider for MYOS_ACTION_COMMAND.")
    action_provider.add_argument("--execute", action="store_true", help="Execute guarded external action instead of dry-run outbox.")
    action_provider.set_defaults(func=cmd_action_provider)

    watch_dir = sub.add_parser("watch-dir", help="Manage folders Autopilot ingests automatically.")
    watch_sub = watch_dir.add_subparsers(dest="watch_action", required=True)
    watch_add = watch_sub.add_parser("add", help="Watch a folder for text/markdown transcripts and notes.")
    watch_add.add_argument("path")
    watch_add.add_argument("--label", default="")
    watch_add.set_defaults(func=cmd_watch_dir)
    watch_list = watch_sub.add_parser("list", help="List watched folders.")
    watch_list.add_argument("--limit", type=int, default=50)
    watch_list.set_defaults(func=cmd_watch_dir)
    watch_pause = watch_sub.add_parser("pause", help="Pause a watched folder.")
    watch_pause.add_argument("--id", type=int, required=True)
    watch_pause.set_defaults(func=cmd_watch_dir)
    watch_resume = watch_sub.add_parser("resume", help="Resume a watched folder.")
    watch_resume.add_argument("--id", type=int, required=True)
    watch_resume.set_defaults(func=cmd_watch_dir)

    watch_scan = sub.add_parser("watch-scan", help="Scan watched folders now.")
    watch_scan.add_argument("--limit", type=int, default=20)
    watch_scan.add_argument("--min-confidence", type=float, default=0.65)
    watch_scan.set_defaults(func=cmd_watch_scan)

    morning = sub.add_parser("morning", help="Simple start of day flow.")
    morning.add_argument("--env-file", default="")
    morning.add_argument("--meeting-hours", type=float, default=0.0)
    morning.set_defaults(func=cmd_morning)

    now = sub.add_parser("now", help="Get one next action now.")
    now.add_argument("--meeting-hours", type=float, default=0.0)
    now.set_defaults(func=cmd_now)

    end = sub.add_parser("end", help="Simple end-of-day close and report.")
    end.set_defaults(func=cmd_end)

    weekly_simple = sub.add_parser("weekly", help="Simple weekly review workflow.")
    weekly_simple.set_defaults(func=cmd_weekly)

    live = sub.add_parser("live", help="Simple live activation flow.")
    live.add_argument("--env-file", default="")
    live.add_argument("--install-launchd", action="store_true")
    live.add_argument("--load-launchd", action="store_true")
    live.set_defaults(func=cmd_live)

    health = sub.add_parser("health", help="Simple health check.")
    health.set_defaults(func=cmd_health)

    ui = sub.add_parser("ui", help="Open simple dashboard server.")
    ui.add_argument("--port", type=int, default=8787)
    ui.set_defaults(func=cmd_ui)

    pulse = sub.add_parser("pulse", help="Run continuous orchestration loop.")
    pulse.add_argument("--env-file", default="")
    pulse.add_argument("--interval-sec", type=int, default=1800)
    pulse.add_argument("--meeting-hours", type=float, default=0.0)
    pulse.add_argument("--once", action="store_true")
    pulse.set_defaults(func=cmd_pulse)

    chat = sub.add_parser("chat", help="Interactive always-on assistant (text). Propose-and-approve.")
    chat.add_argument("--backend", default="", help="claude|copilot|cursor|command (default: MYOS_AGENT_BACKEND or claude).")
    chat.add_argument("--env-file", default="")
    chat.set_defaults(func=cmd_chat)

    voice = sub.add_parser("voice", help="Interactive always-on assistant (push-to-talk voice).")
    voice.add_argument("--backend", default="", help="claude|copilot|cursor|command (default: MYOS_AGENT_BACKEND or claude).")
    voice.add_argument("--env-file", default="")
    voice.add_argument("--text-reply", action="store_true", help="Print replies without speaking them.")
    voice.set_defaults(func=cmd_voice)

    team = sub.add_parser("team", help="List or add team members / stakeholders.")
    team_sub = team.add_subparsers(dest="team_action")
    team_add = team_sub.add_parser("add", help="Add or update a person.")
    team_add.add_argument("name")
    team_add.add_argument("--role", default="")
    team_add.add_argument("--team", default="")
    team_add.add_argument("--relation", choices=["report", "peer", "stakeholder", "manager"], default="report")
    team_add.set_defaults(func=cmd_team)
    team.set_defaults(func=cmd_team)

    note = sub.add_parser("note", help="Capture free-form text; MYOS infers what it is (evidence/1:1/meeting/decision/risk/note) and files it.")
    note.add_argument("text")
    note.set_defaults(func=cmd_note)

    one_on_one = sub.add_parser("1on1", help="Log a 1:1; action items are extracted to your inbox.")
    one_on_one.add_argument("--person", required=True)
    one_on_one.add_argument("notes")
    one_on_one.set_defaults(func=cmd_one_on_one)

    meeting = sub.add_parser("meeting", help="Capture a meeting (notes or --audio); decisions + action items extracted.")
    meeting.add_argument("text", nargs="?", default="")
    meeting.add_argument("--title", default="")
    meeting.add_argument("--audio", default="", help="Audio file to transcribe (needs faster-whisper).")
    meeting.set_defaults(func=cmd_meeting)

    review_draft = sub.add_parser("review-draft", help="Assemble a performance-review packet for a person.")
    review_draft.add_argument("--person", required=True)
    review_draft.set_defaults(func=cmd_review_draft)

    risk_scan = sub.add_parser("risk-scan", help="Scan synced Jira/GitHub + work items for risks; optionally draft nudges.")
    risk_scan.add_argument("--risk-threshold", type=int, default=60)
    risk_scan.add_argument("--limit", type=int, default=25)
    risk_scan.add_argument("--draft-nudges", action="store_true", help="Enqueue a nudge proposal per finding (approval-gated).")
    risk_scan.add_argument("--nudge-limit", type=int, default=10)
    risk_scan.set_defaults(func=cmd_risk_scan)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
