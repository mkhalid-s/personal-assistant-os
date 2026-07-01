from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import sqlite3
import sys
import time
import uuid
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime
from pathlib import Path
import subprocess

from .connectors import AhaConnector, ConfluenceConnector, GitHubConnector, JiraConnector
from .dashboard import render_dashboard_html, serve_dashboard
from .db import append_event, get_connection, initialize_schema, resolve_db_path, verify_schema
from .extraction import extract_suggestions
from .graph import connect_work_items
from .ingest.audio import transcribe_audio
from .ingest.image import extract_image_text
from .pulse import detect_mode, run_cycle
from .retrieval import hybrid_score
from . import assistant, autonomy, claims, command_registry, context as ctx, em, entities, factory, graphrag, intents, model_setup, observability, plans, providers, queries, relationships, router, watch
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
    _handle_proposals, approve_and_execute, execute_connector_mutation,
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


def _command_path(args: argparse.Namespace) -> str:
    parts = [str(getattr(args, "command", "") or "unknown")]
    for name, value in sorted(vars(args).items()):
        if name.endswith("_action") and isinstance(value, str) and value:
            parts.append(value)
    return " ".join(parts)


def _argv_hash(argv: list[str]) -> str:
    return observability._hash_text("\0".join(argv))  # hashed only; raw args may contain private text


def _trace_enabled_for(args: argparse.Namespace) -> bool:
    # These commands create, move, or select the database itself; opening an
    # observability connection before they run can interfere with their purpose.
    return str(getattr(args, "command", "") or "") not in {"restore", "setup-live"}


def _command_autonomy_decision(conn: sqlite3.Connection, command: str, *, requested_mode: str = "") -> dict[str, object]:
    spec = command_registry.find_command(command)
    return autonomy.decide_command(
        command,
        safety=spec.safety if spec else "unknown",
        requires_confirmation=bool(spec.requires_confirmation) if spec else True,
        level=autonomy.level_from_policy(conn),
        requested_mode=requested_mode,
    )


def _print_autonomy_decision(decision: dict[str, object]) -> None:
    print(
        "Autonomy: "
        f"decision={decision['decision']} tier={decision['tier']} "
        f"safety={decision['safety']} reason={decision['reason']}"
    )


def _print_recommendations(recommendations: list[dict[str, object]]) -> None:
    for item in recommendations[:2]:
        command = str(item.get("command") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if command:
            reason = reason.rstrip(".")
        suffix = f" -> {command}" if command else ""
        print(f"Recommendation: {reason}{suffix}")


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
    open_intents = conn.execute("SELECT COUNT(*) AS c FROM intents WHERE status = 'open'").fetchone()["c"]
    pending_approvals = conn.execute(
        "SELECT COUNT(*) AS c FROM agent_actions WHERE status = 'proposed'"
    ).fetchone()["c"]
    active_factory_runs = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM factory_runs
        WHERE status IN ('running', 'awaiting_approval', 'execution_ready', 'approved_for_execution')
        """
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
        json.dumps(
            {
                "mode": mode,
                "open_items": open_count,
                "high_risk": high_risk,
                "open_intents": open_intents,
                "pending_approvals": pending_approvals,
                "active_factory_runs": active_factory_runs,
            },
            ensure_ascii=True,
        ),
    )
    conn.commit()

    print("Day closed.")
    print(summary)
    print(f"Open intents: {open_intents}")
    print(f"Pending approvals: {pending_approvals}")
    print(f"Active factory runs: {active_factory_runs}")
    if args.note:
        print(f"Note: {args.note}")


def cmd_morning_brief(args: argparse.Namespace) -> None:
    conn = get_connection()
    print("Morning brief:")
    intents_rows = conn.execute(
        """
        SELECT id, objective, priority
        FROM intents
        WHERE status = 'open'
        ORDER BY priority ASC, id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print("Priorities:")
    if intents_rows:
        for row in intents_rows:
            print(f"- intent #{row['id']} priority={row['priority']} {row['objective']}")
    else:
        print("- none")

    risks = conn.execute(
        """
        SELECT id, title, risk_score, due_date
        FROM work_items
        WHERE status = 'open' AND risk_score >= ?
        ORDER BY risk_score DESC, id DESC
        LIMIT ?
        """,
        (args.risk_threshold, args.limit),
    ).fetchall()
    print("Risks:")
    if risks:
        for row in risks:
            due = row["due_date"] or "no due date"
            print(f"- work_item #{row['id']} risk={row['risk_score']} due={due} {row['title']}")
    else:
        print("- none")

    approvals = conn.execute(
        """
        SELECT id, title, action_type
        FROM agent_actions
        WHERE status = 'proposed'
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print("Pending approvals:")
    if approvals:
        for row in approvals:
            print(f"- action #{row['id']} [{row['action_type']}] {row['title']}")
    else:
        print("- none")

    factory_rows = conn.execute(
        """
        SELECT id, intent_id, mode, workflow_pack, status
        FROM factory_runs
        WHERE status IN ('running', 'awaiting_approval', 'execution_ready', 'approved_for_execution')
        ORDER BY started_at DESC, id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print("Factory runs:")
    if factory_rows:
        for row in factory_rows:
            print(
                f"- factory #{row['id']} intent=#{row['intent_id']} mode={row['mode']} "
                f"pack={row['workflow_pack']} status={row['status']}"
            )
    else:
        print("- none")

    evidence_gaps = conn.execute(
        """
        SELECT i.id, i.objective
        FROM intents i
        LEFT JOIN intent_evidence e ON e.intent_id = i.id
        WHERE i.status = 'open'
        GROUP BY i.id
        HAVING COUNT(e.id) = 0
        ORDER BY i.priority ASC, i.id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print("Evidence gaps:")
    if evidence_gaps:
        for row in evidence_gaps:
            print(f"- intent #{row['id']} needs evidence: {row['objective']}")
    else:
        print("- none")


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
    if getattr(args, "graph", False):
        hits = graphrag.retrieve(
            conn,
            args.query,
            limit=args.limit,
            graph_hops=args.graph_hops,
            record_run=True,
            mode="context_graph",
        )
        conn.commit()
        if not hits:
            print("No relevant graph context found.")
            return
        print(f"Graph context results for: {args.query}")
        print(f"retrieval run: #{hits[0]['retrieval_run_id']}")
        for hit in hits:
            snippet = str(hit["content"]).strip().replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            print(f"- ({hit['score']:.3f}) {hit['citation']}: {snippet}")
            print(f"  reason: {hit['reason']}")
            if hit["graph_path"]:
                print(f"  path: {' -> '.join(hit['graph_path'])}")
        return

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


def cmd_retrieval_run(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "retrieval_run_action", "list") or "list"
    if action == "show":
        if args.id is None:
            print("Usage: myos retrieval-run show --id N")
            raise SystemExit(1)
        run = conn.execute(
            """
            SELECT id, query, mode, limit_requested, graph_hops, candidate_limit, selected_count, created_at
            FROM retrieval_runs
            WHERE id = ?
            """,
            (args.id,),
        ).fetchone()
        if not run:
            print("Retrieval run not found.")
            return
        print(f"Retrieval run #{run['id']} [{run['mode']}]")
        print(f"query: {run['query']}")
        print(
            f"requested: limit={run['limit_requested']} graph_hops={run['graph_hops']} "
            f"candidates={run['candidate_limit']} selected={run['selected_count']}"
        )
        print(f"created: {run['created_at']}")
        sources = conn.execute(
            """
            SELECT rank, citation, score, reason, graph_path_json, content_preview
            FROM retrieval_run_sources
            WHERE retrieval_run_id = ?
            ORDER BY rank ASC
            """,
            (run["id"],),
        ).fetchall()
        if not sources:
            print("sources: none")
            return
        print("sources:")
        for source in sources:
            preview = source["content_preview"] or ""
            print(f"{source['rank']}. ({source['score']:.3f}) {source['citation']}: {preview}")
            print(f"   reason: {source['reason']}")
            path = json.loads(source["graph_path_json"] or "[]")
            if path:
                print(f"   path: {' -> '.join(path)}")
        return

    rows = conn.execute(
        """
        SELECT id, query, mode, selected_count, created_at
        FROM retrieval_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No retrieval runs recorded.")
        return
    print("Retrieval runs:")
    for row in rows:
        print(
            f"- #{row['id']} [{row['mode']}] {row['query']} "
            f"(sources={row['selected_count']}, created={row['created_at']})"
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


def _sqlite_fts5_available(conn: sqlite3.Connection) -> tuple[bool, str]:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.myos_fts_check USING fts5(content)")
        conn.execute("DROP TABLE temp.myos_fts_check")
        return True, "FTS5 available"
    except sqlite3.Error as exc:
        return False, str(exc)


def _repo_file(path: str) -> Path:
    return Path(__file__).resolve().parents[2] / path


def cmd_doctor(args: argparse.Namespace) -> None:
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

    core_checks: list[tuple[str, bool, str]] = []
    optional_checks: list[tuple[str, bool, str]] = []

    db_path = resolve_db_path()
    db_parent = db_path.expanduser().parent
    fts_ok, fts_detail = _sqlite_fts5_available(conn)
    schema_status = verify_schema(conn)
    gitignore_text = _repo_file(".gitignore").read_text() if _repo_file(".gitignore").exists() else ""

    core_checks.extend(
        [
            (
                "python_version",
                sys.version_info >= (3, 10),
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            ),
            (
                "package_import",
                importlib.util.find_spec("personal_assistant") is not None,
                "personal_assistant importable",
            ),
            ("db_connection", conn.execute("SELECT 1").fetchone() is not None, str(db_path)),
            (
                "db_parent_writable",
                db_parent.exists() and os.access(db_parent, os.W_OK),
                str(db_parent),
            ),
            ("sqlite_fts5", fts_ok, fts_detail),
            (
                "schema_migrations",
                bool(schema_status["ok"]),
                f"current={schema_status['current_version']} expected={schema_status['expected_version']}",
            ),
            ("env_example", _repo_file(".env.example").exists(), str(_repo_file(".env.example"))),
            (
                "local_artifacts_ignored",
                "data/" in gitignore_text and ".env" in gitignore_text,
                ".gitignore covers data and env files",
            ),
        ]
    )

    credential_groups = {
        "jira_credentials": ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"],
        "github_credentials": ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"],
        "confluence_credentials": ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        "aha_credentials": ["AHA_BASE_URL", "AHA_API_TOKEN"],
    }
    for name, keys in credential_groups.items():
        missing = [key for key in keys if not os.getenv(key, "").strip()]
        optional_checks.append((name, not missing, "ready" if not missing else "missing " + ", ".join(missing)))

    optional_checks.extend(
        [
            ("tesseract", bool(shutil.which("tesseract")), shutil.which("tesseract") or "not installed"),
            ("launchctl", bool(shutil.which("launchctl")), shutil.which("launchctl") or "not available"),
            (
                "action_provider",
                bool(os.getenv("MYOS_ACTION_COMMAND", "").strip()),
                os.getenv("MYOS_ACTION_COMMAND", "") or "not configured",
            ),
        ]
    )
    router_status = model_setup.router_status()
    optional_checks.append(
        (
            "router_model",
            bool(router_status["available"]),
            f"{router_status['backend']} {router_status['model']} ({router_status['detail']})",
        )
    )

    print("Core checks:")
    for name, ok, detail in core_checks:
        print(f"- {'PASS' if ok else 'FAIL'} {name}: {detail}")

    print("Optional checks:")
    for name, ok, detail in optional_checks:
        print(f"- {'PASS' if ok else 'INFO'} {name}: {detail}")

    print(f"Autonomy level: {autonomy.level_from_policy(conn)} (auto-run safe / one-tap non-destructive / block destructive)")
    active = providers.resolve_backend_name()
    print(f"Agent backends (active: {active}):")
    for b in providers.available_backends():
        mark = "PASS" if b["available"] else "INFO"
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
        if args.strict and any(not ok for _, ok, _ in core_checks):
            print("Doctor strict: core checks failed.")
            raise SystemExit(1)
        if args.strict:
            print("Doctor strict: core checks passed.")
        return
    print("Connector status:")
    for row in rows:
        err = f" err={row['last_error']}" if row["last_error"] else ""
        print(
            f"- {row['connector']}: status={row['last_status']} "
            f"last_success={row['last_success_at']}{err}"
        )
    if args.strict and any(not ok for _, ok, _ in core_checks):
        print("Doctor strict: core checks failed.")
        raise SystemExit(1)
    if args.strict:
        print("Doctor strict: core checks passed.")


def _check_sqlite_file(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"missing {path}"
    try:
        conn = sqlite3.connect(path)
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        has_migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return False, f"sqlite error: {exc}"
    if quick_check != "ok":
        return False, f"quick_check={quick_check}"
    if not has_migrations:
        return False, "schema_migrations table missing"
    return True, "ok"


def cmd_migrations(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "migrations_action", "verify") or "verify"
    if action == "list":
        rows = conn.execute(
            """
            SELECT version, name, applied_at
            FROM schema_migrations
            ORDER BY version ASC
            """
        ).fetchall()
        print("Schema migrations:")
        for row in rows:
            print(f"- {row['version']:02d} {row['name']} applied_at={row['applied_at']}")
        status = verify_schema(conn)
        print(f"Current version: {status['current_version']} / expected {status['expected_version']}")
        return

    status = verify_schema(conn)
    print("Migration verification:")
    print(f"- current_version={status['current_version']} expected={status['expected_version']}")
    print(f"- quick_check={status['quick_check']}")
    print(f"- foreign_key_violations={status['foreign_key_violations']}")
    missing_versions = status["missing_versions"]
    missing_tables = status["missing_tables"]
    print(f"- missing_versions={missing_versions if missing_versions else 'none'}")
    print(f"- missing_tables={missing_tables if missing_tables else 'none'}")
    if not status["ok"]:
        print("Schema migrations verification failed.")
        if getattr(args, "strict", False):
            raise SystemExit(1)
        return
    print("Schema migrations verified.")


def cmd_backup(args: argparse.Namespace) -> None:
    source = resolve_db_path()
    conn = get_connection()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = Path(args.output).expanduser() if args.output else source.parent / "backups" / f"assistant-{timestamp}.db"
    output.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(output)
    try:
        conn.backup(dest)
    finally:
        dest.close()
        conn.close()
    print(f"Backup created: {output}")


def cmd_restore(args: argparse.Namespace) -> None:
    source = Path(args.source).expanduser()
    ok, detail = _check_sqlite_file(source)
    if not ok:
        print(f"Restore refused: {detail}")
        raise SystemExit(1)

    target = resolve_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety_backup = target.parent / "backups" / f"pre-restore-{timestamp}.db"
        safety_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, safety_backup)
        print(f"Current database backed up: {safety_backup}")
    shutil.copy2(source, target)
    for sidecar in (target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()

    conn = get_connection()
    status = verify_schema(conn)
    conn.close()
    if not status["ok"]:
        print("Restore completed, but schema verification failed.")
        raise SystemExit(1)
    print(f"Database restored from: {source}")
    print("Schema migrations verified.")


def _pyproject_dependencies(pyproject: Path) -> list[str]:
    if not pyproject.exists():
        return []
    deps: list[str] = []
    in_deps = False
    for line in pyproject.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("dependencies"):
            if "[" in stripped and "]" in stripped:
                raw = stripped.split("[", 1)[1].rsplit("]", 1)[0]
                return [
                    item.strip().strip("'").strip('"')
                    for item in raw.split(",")
                    if item.strip().strip("'").strip('"')
                ]
            in_deps = True
            continue
        if in_deps and stripped.startswith("]"):
            break
        if in_deps:
            value = stripped.strip(",").strip("'").strip('"')
            if value:
                deps.append(value)
    return deps


def cmd_dependency_check(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    license_file = root / "LICENSE"
    text = pyproject.read_text() if pyproject.exists() else ""
    deps = _pyproject_dependencies(pyproject)
    checks = [
        ("pyproject", pyproject.exists(), str(pyproject)),
        ("license_metadata", "Apache-2.0" in text, "Apache-2.0 in pyproject"),
        ("license_file", license_file.exists() and "Apache License" in license_file.read_text(), str(license_file)),
    ]
    print("Dependency and license check:")
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"- {'PASS' if passed else 'FAIL'} {name}: {detail}")
    print(f"- dependencies={len(deps)}")
    for dep in deps:
        print(f"  - {dep}")
    if args.strict and not ok:
        raise SystemExit(1)


def cmd_performance_baseline(args: argparse.Namespace) -> None:
    conn = get_connection()
    start = time.monotonic()
    hits = graphrag.retrieve(conn, args.query, limit=args.limit)
    retrieval_ms = int((time.monotonic() - start) * 1000)

    start = time.monotonic()
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM intents WHERE status='open') AS open_intents,
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_work,
          (SELECT COUNT(*) FROM agent_actions WHERE status='proposed') AS pending_approvals,
          (SELECT COUNT(*) FROM retrieval_runs) AS retrieval_runs
        """
    ).fetchone()
    summary_ms = int((time.monotonic() - start) * 1000)

    print("Performance baseline:")
    print(f"- retrieval_ms={retrieval_ms} query={args.query!r} hits={len(hits)}")
    print(
        f"- readiness_query_ms={summary_ms} open_intents={counts['open_intents']} "
        f"open_work={counts['open_work']} pending_approvals={counts['pending_approvals']} "
        f"retrieval_runs={counts['retrieval_runs']}"
    )


def _release_scan_files(root: Path) -> list[Path]:
    scan_roots = [
        "README.md",
        "ARCHITECTURE.md",
        "ROADMAP.md",
        "pyproject.toml",
        "src",
        "tests",
        "docs",
        ".github",
    ]
    files: list[Path] = []
    for rel in scan_roots:
        path = root / rel
        if path.is_file():
            files.append(path)
        elif path.exists():
            files.extend(
                p for p in path.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            )
    return files


def _release_hygiene_findings(root: Path) -> list[str]:
    patterns = [
        "Guide" + "wire",
        "GW Bed" + "rock",
        "Co-authored-" + "by",
        "Cur" + "sor",
        "/Users/" + "mshaikh",
        "Documents/" + "GW",
        "personal-assistant-os-" + "public",
    ]
    findings: list[str] = []
    for path in _release_scan_files(root):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern in patterns:
                if pattern in line:
                    findings.append(f"{path.relative_to(root)}:{line_no}: {pattern}")
    return findings


def _tracked_local_artifacts(root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    blocked: list[str] = []
    for raw in proc.stdout.splitlines():
        path = raw.strip()
        name = Path(path).name
        if (
            name in {".env", ".DS_Store"}
            or path.startswith((".cursor/", ".claude/"))
            or Path(path).suffix in {".db", ".sqlite", ".sqlite3", ".log"}
        ):
            blocked.append(path)
    return blocked


def _factory_release_smoke(conn: sqlite3.Connection) -> tuple[bool, str]:
    smoke_conn = sqlite3.connect(":memory:")
    smoke_conn.row_factory = sqlite3.Row
    initialize_schema(smoke_conn)
    try:
        intent_id = intents.create_intent(
            smoke_conn,
            objective="Release smoke: verify review-first factory trace",
            context="Local release-check smoke test.",
            success_criteria="Factory run creates plan, retrieval, review packet, and role artifacts.",
        )
        result = factory.start_review_first_run(smoke_conn, intent_id=intent_id, mode="review_first")
        artifacts = smoke_conn.execute(
            """
            SELECT artifact_type, COUNT(*) AS c
            FROM factory_artifacts
            WHERE factory_run_id = ?
            GROUP BY artifact_type
            """,
            (int(result["id"]),),
        ).fetchall()
        counts = {row["artifact_type"]: int(row["c"]) for row in artifacts}
        required = {
            "plan": 1,
            "retrieval_run": 1,
            "review_packet": 1,
            "agent_run": 5,
        }
        missing = [name for name, min_count in required.items() if counts.get(name, 0) < min_count]
        semi_intent_id = intents.create_intent(
            smoke_conn,
            objective="Release smoke: verify semi-autonomous local receipt",
            context="Local release-check smoke test.",
            success_criteria="Safe local action executes with receipt.",
        )
        factory.set_policy(smoke_conn, allowed_mode="semi_autonomous", scope_type="intent", scope_id=str(semi_intent_id))
        semi = factory.start_review_first_run(smoke_conn, intent_id=semi_intent_id, mode="semi_autonomous")
        receipt_count = smoke_conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM factory_artifacts
            WHERE factory_run_id = ? AND artifact_type = 'execution_receipt'
            """,
            (int(semi["id"]),),
        ).fetchone()["c"]
        connector_intent_id = intents.create_intent(
            smoke_conn,
            objective="Release smoke: verify connector dry-run receipt",
            context="Local release-check connector smoke test.",
            success_criteria="Connector dry-run creates outbox and receipt.",
        )
        smoke_conn.execute(
            """
            INSERT INTO external_items (connector, external_id, item_type, title, body, url)
            VALUES ('confluence', 'PAGE-SMOKE', 'page', 'Connector smoke page', 'Needs dry-run update.', 'https://example.test/wiki/PAGE-SMOKE')
            """
        )
        intents.add_evidence(
            smoke_conn,
            intent_id=connector_intent_id,
            content="confluence:PAGE-SMOKE page Connector smoke page",
            source_type="external_item",
            source_id="1",
            summary="confluence page: Connector smoke page",
            confidence=0.8,
        )
        factory.set_policy(smoke_conn, allowed_mode="full_autonomous", scope_type="intent", scope_id=str(connector_intent_id))
        factory.set_policy(
            smoke_conn,
            allowed_mode="full_autonomous",
            connector="confluence",
            action_type="draft_external_update",
        )
        connector = factory.start_review_first_run(
            smoke_conn,
            intent_id=connector_intent_id,
            mode="full_autonomous",
            workflow_pack="connector_ops",
        )
        connector_outbox = smoke_conn.execute(
            "SELECT COUNT(*) AS c FROM action_outbox WHERE target_type='confluence' AND target_ref='PAGE-SMOKE'"
        ).fetchone()["c"]
        ok = (
            not missing
            and result["status"] == "awaiting_approval"
            and semi["status"] == "execution_completed"
            and int(receipt_count) >= 1
            and connector["status"] == "execution_completed"
            and int(connector_outbox) >= 1
        )
        detail = (
            "review-first trace, semi-autonomous receipt, and connector dry-run ok"
            if ok
            else (
                f"missing={','.join(missing)} status={result['status']} semi={semi['status']} "
                f"receipts={receipt_count} connector={connector['status']} outbox={connector_outbox}"
            )
        )
        return ok, detail
    except Exception as exc:
        return False, str(exc)
    finally:
        smoke_conn.close()


def cmd_release_check(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[2]
    conn = get_connection()
    schema = verify_schema(conn)
    hygiene = _release_hygiene_findings(root)
    artifacts = _tracked_local_artifacts(root)
    factory_smoke_ok, factory_smoke_detail = _factory_release_smoke(conn)
    required_files = [
        root / "LICENSE",
        root / "README.md",
        root / "CHANGELOG.md",
        root / "docs" / "MIGRATIONS.md",
        root / "docs" / "RECOVERY.md",
        root / ".github" / "workflows" / "ci.yml",
        root / ".github" / "workflows" / "release.yml",
    ]
    dependency_ok = "Apache-2.0" in (root / "pyproject.toml").read_text(errors="ignore")
    checks = [
        ("schema", bool(schema["ok"]), f"current={schema['current_version']} expected={schema['expected_version']}"),
        ("dependency_license", dependency_ok, "Apache-2.0 metadata"),
        ("required_files", all(path.exists() for path in required_files), "docs, changelog, license, workflows"),
        ("public_hygiene", not hygiene, f"{len(hygiene)} finding(s)"),
        ("local_artifacts", not artifacts, f"{len(artifacts)} tracked local artifact(s)"),
        ("factory_smoke", factory_smoke_ok, factory_smoke_detail),
    ]
    print("Release readiness check:")
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"- {'PASS' if passed else 'FAIL'} {name}: {detail}")
    if hygiene and args.verbose:
        print("Hygiene findings:")
        for finding in hygiene[:20]:
            print(f"- {finding}")
    if artifacts and args.verbose:
        print("Tracked local artifacts:")
        for artifact in artifacts[:20]:
            print(f"- {artifact}")
    if args.strict and not ok:
        raise SystemExit(1)


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
    if getattr(args, "graph", False):
        hits = graphrag.retrieve(
            conn,
            row["title"],
            limit=args.limit,
            graph_hops=args.graph_hops,
            record_run=True,
            mode="why_graph",
        )
        conn.commit()
        evidence = [
            hit for hit in hits
            if hit["graph_path"] or hit["source_type"] != "work_item" or int(hit["source_id"]) != int(row["id"])
        ]
        if not evidence:
            print("graph: no related evidence found.")
            return
        print(f"retrieval run: #{hits[0]['retrieval_run_id']}")
        print("graph evidence:")
        for hit in evidence:
            snippet = str(hit["content"]).strip().replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            print(f"- ({hit['score']:.3f}) {hit['citation']}: {snippet}")
            print(f"  reason: {hit['reason']}")
            if hit["graph_path"]:
                print(f"  path: {' -> '.join(hit['graph_path'])}")


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
            "# Optional tiny local router model for intent finding",
            "MYOS_ROUTER_BACKEND=",
            "MYOS_ROUTER_MODEL=",
            "MYOS_ROUTER_COMMAND=",
            "MYOS_ROUTER_TIMEOUT_SEC=8",
            "MYOS_ROUTER_MIN_CONFIDENCE=0.70",
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


def _upsert_env_lines(path: Path, lines: list[str], *, header: str = "# Managed tiny router model") -> None:
    keys = {line.split("=", 1)[0].strip() for line in lines if "=" in line}
    existing = path.read_text().splitlines() if path.exists() else []
    kept = []
    for line in existing:
        raw = line.strip()
        candidate = raw[len("export ") :].strip() if raw.startswith("export ") else raw
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key in keys:
            continue
        kept.append(line)
    if kept and kept[-1].strip():
        kept.append("")
    kept.append(header)
    kept.extend(lines)
    path.write_text("\n".join(kept).rstrip() + "\n")


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
    router_model_plan = None
    if getattr(args, "router_model", False):
        try:
            router_model_plan = model_setup.setup_plan(
                runtime=getattr(args, "router_runtime", "auto"),
                model=getattr(args, "router_model_name", ""),
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
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
    if router_model_plan:
        _print_model_plan(router_model_plan)
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
    if router_model_plan:
        setup_result = model_setup.apply_setup(router_model_plan, dry_run=False)
        _upsert_env_lines(env_path, list(router_model_plan["env_lines"]))
        env_path.chmod(0o600)
        print(f"Router model setup: {setup_result['status']}")
        if setup_result.get("wrapper"):
            print(f"Router wrapper: {setup_result['wrapper']}")
        if setup_result["status"] == "failed":
            print(setup_result.get("stderr") or setup_result.get("stdout") or "model setup failed")
            raise SystemExit(1)

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
    intent_count = conn.execute("SELECT COUNT(*) AS c FROM intents WHERE status='open'").fetchone()["c"]
    evidence_gap_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM intents i
        WHERE i.status = 'open'
          AND NOT EXISTS (SELECT 1 FROM intent_evidence e WHERE e.intent_id = i.id)
        """
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
    print(f"- open_intents={intent_count} evidence_gaps={evidence_gap_count}")
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
        launchctl = shutil.which("launchctl")
        if not launchctl:
            print("launchctl unavailable; copied files but skipped loading launch agents.")
            return
        subprocess.run([launchctl, "unload", str(dst_sync)], check=False)
        subprocess.run([launchctl, "unload", str(dst_pulse)], check=False)
        if args.autopilot:
            subprocess.run([launchctl, "unload", str(dst_autopilot)], check=False)
        subprocess.run([launchctl, "load", str(dst_sync)], check=False)
        subprocess.run([launchctl, "load", str(dst_pulse)], check=False)
        if args.autopilot:
            subprocess.run([launchctl, "load", str(dst_autopilot)], check=False)
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
    launchctl = shutil.which("launchctl")
    if not launchctl:
        for label in labels:
            print(f"- {label}: unavailable (launchctl not found)")
        return
    for label in labels:
        proc = subprocess.run(
            [launchctl, "list", label],
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
    if not getattr(args, "run_day", False) and not getattr(args, "env_file", ""):
        cmd_morning_brief(args)
        return
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
    cmd_doctor(argparse.Namespace(strict=False))


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
        if target in {"jira", "github", "confluence", "aha"} or payload.get("operation"):
            result = execute_connector_mutation(
                conn,
                agent_action_id=agent_action_id,
                action_type=action_type,
                title=title,
                payload=payload,
                approved=approved,
                execute_live=bool(args.execute),
            )
            conn.commit()
            if result["status"] in {"blocked", "failed"}:
                print(json.dumps({"status": result["status"], "error": result.get("error", "")}, ensure_ascii=True))
                raise SystemExit(1)
            print(json.dumps(result, ensure_ascii=True))
            return

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
            rollback = payload.get("rollback_note") or payload.get("rollback")
            if rollback:
                print(f"  rollback: {rollback}")
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


def cmd_do(args: argparse.Namespace) -> None:
    conn = get_connection()
    route_decision = router.route_with_feedback(conn, args.text, surface="do")
    autonomy_decision = router.autonomy_decision_for_route(conn, route_decision)
    _print_autonomy_decision(autonomy_decision)
    _print_recommendations(
        autonomy.recommend_next_steps(
            autonomy_decision,
            command="do",
            intent=route_decision.intent,
            workflow_pack=route_decision.workflow_pack,
        )
    )
    if autonomy_decision["decision"] == autonomy.BLOCKED:
        raise SystemExit(1)
    result = router.execute_route(conn, args.text, surface="do", decision=route_decision)
    result["autonomy"] = autonomy_decision
    conn.commit()
    print(router.summarize_result(result))
    decision = result["decision"]
    if decision.get("requires_confirmation"):
        print("Safety: route is review-first or clarification-oriented; external mutations remain approval-gated.")


def _print_model_plan(plan: dict[str, object]) -> None:
    print("Router model setup plan:")
    print(f"- runtime: {plan['runtime']} ({'available' if plan['runtime_available'] else 'not available'})")
    print(f"- runtime_detail: {plan['runtime_detail']}")
    print(f"- model: {plan['model']} ({plan['model_label']})")
    print(f"- footprint: {plan['footprint']}")
    print(f"- quality: {plan['quality']}")
    print(f"- pull_command: {plan['pull_command_text']}")
    print(f"- wrapper_path: {plan['wrapper_path']}")
    print("- env:")
    for line in plan["env_lines"]:
        print(f"  {line}")
    print(f"- privacy: {plan['privacy_note']}")


def cmd_model(args: argparse.Namespace) -> None:
    action = getattr(args, "model_action", "")
    if action == "recommend":
        try:
            rec = model_setup.recommended_model(args.purpose)
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        print(f"Recommended {rec['purpose']} model: {rec['model']} ({rec['label']})")
        print(f"- footprint: {rec['footprint']}")
        print(f"- quality: {rec['quality']}")
        return
    if action == "status":
        status = model_setup.router_status()
        print("Router model status:")
        print(f"- backend: {status['backend']}")
        print(f"- model: {status['model']}")
        print(f"- command: {status['command']}")
        print(f"- runtime: {status['runtime']}")
        print(f"- available: {bool(status['available'])}")
        print(f"- detail: {status['detail']}")
        return
    if action == "setup":
        if not args.router:
            print("Only router model setup is supported in this release. Use --router.")
            raise SystemExit(1)
        try:
            plan = model_setup.setup_plan(runtime=args.runtime, model=args.model, command=args.command)
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        _print_model_plan(plan)
        result = model_setup.apply_setup(plan, dry_run=not args.apply)
        if not args.apply:
            print("Dry run only. Re-run with --apply to pull the model and write the wrapper.")
            return
        print(f"Apply status: {result['status']}")
        if result.get("wrapper"):
            print(f"Wrapper written: {result['wrapper']}")
        if result.get("stdout"):
            print(f"stdout: {result['stdout']}")
        if result.get("stderr"):
            print(f"stderr: {result['stderr']}")
        if result["status"] == "failed":
            raise SystemExit(1)
        return
    raise SystemExit("Unknown model command.")


def cmd_router(args: argparse.Namespace) -> None:
    action = getattr(args, "router_action", "")
    if action == "eval":
        try:
            result = router.evaluate_routes(
                fixture_path=args.fixture or None,
                model_shadow=args.model_shadow,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Router eval failed: {exc}")
            raise SystemExit(1) from exc
        conn = get_connection()
        run_id = 0
        if not args.no_record:
            run_id = router.record_route_eval(conn, result)
        summary = result["summary"]
        print("Router eval:")
        print(f"- fixtures: {summary['total']} from {result['fixture_path']}")
        print(f"- passed: {summary['passed']} failed={summary['failed']} accuracy={summary['accuracy']:.2%}")
        print(f"- low_confidence: {summary['low_confidence']}")
        if args.model_shadow:
            print(
                f"- model_shadow: overrides={summary['model_overrides']} "
                f"wins={summary['model_wins']} losses={summary['model_losses']}"
            )
        print(f"- calibration: {summary['calibration']}")
        if run_id:
            print(f"- recorded_eval_run: #{run_id}")
        failures = [case for case in result["cases"] if not case["passed"]]
        if failures:
            print("Failures:")
            for case in failures[:10]:
                print(
                    f"- {case['fixture_id']}: expected={case['expected_intent']} "
                    f"actual={case['actual_intent']} confidence={case['confidence']:.2f}"
                )
        return
    if action == "feedback":
        conn = get_connection()
        try:
            feedback_id = router.record_route_feedback(
                conn,
                event_id=args.event,
                expected_intent=args.expected_intent,
                note=args.note or "",
            )
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"Router feedback failed: {exc}")
            raise SystemExit(1) from exc
        print(f"Router feedback recorded: #{feedback_id}")
        print("Privacy: note text was hashed; raw request text was not stored.")
        return
    if action == "overrides":
        conn = get_connection()
        rows = router.list_route_overrides(conn, limit=args.limit)
        if not rows:
            print("No active router overrides.")
            return
        print("Router overrides:")
        for row in rows:
            print(
                f"- #{row['id']} intent={row['expected_intent']} status={row['status']} "
                f"hash={row['text_hash'][:12]} feedback=#{row['source_feedback_id'] or 'none'} "
                f"updated={row['updated_at']}"
            )
        return
    if action == "commands":
        specs = command_registry.filter_commands(
            tier=args.tier or "",
            safety=args.safety or "",
            intent=args.intent or "",
        )
        print("Router command registry:")
        if not specs:
            print("- no commands match filters")
            return
        for spec in specs[: args.limit]:
            confirm = " confirmation=yes" if spec.requires_confirmation else ""
            required = f" required={','.join(spec.required_args)}" if spec.required_args else ""
            example = f" example={spec.examples[0]}" if spec.examples else ""
            print(
                f"- myos {spec.command} tier={spec.tier} safety={spec.safety} "
                f"intent={spec.intent}{confirm}{required}{example}"
            )
        return
    raise SystemExit("Unknown router command.")


def cmd_trace(args: argparse.Namespace) -> None:
    action = getattr(args, "trace_action", "")
    conn = get_connection()
    if action == "list":
        rows = observability.list_traces(
            conn,
            limit=args.limit,
            status=args.status or "",
            command=args.command_filter or "",
        )
        current_trace = observability.current_correlation_id()
        if current_trace:
            rows = [row for row in rows if row.get("correlation_id") != current_trace]
        if not rows:
            print("No execution traces.")
            return
        print("Execution traces:")
        for row in rows:
            links = []
            if row.get("route_event_id"):
                links.append(f"route_event=#{row['route_event_id']}")
            if row.get("factory_run_id"):
                links.append(f"factory_run=#{row['factory_run_id']}")
            if row.get("agent_task_id"):
                links.append(f"agent_task=#{row['agent_task_id']}")
            if row.get("receipt_id"):
                links.append(f"receipt=#{row['receipt_id']}")
            link_text = f" {' '.join(links)}" if links else ""
            print(
                f"- #{row['id']} {row['command_path']} status={row['status']} "
                f"duration_ms={row['duration_ms']} corr={row['correlation_id']}{link_text}"
            )
        return
    if action == "cleanup":
        result = observability.cleanup_traces(
            conn,
            retention_days=args.retention_days,
            max_rows=args.max_rows,
        )
        print(
            "Trace cleanup: "
            f"rolled_up={result['rolled_up']} deleted={result['deleted']} remaining={result['remaining']}"
        )
        return
    if action == "rollups":
        rows = observability.rollups(conn, limit=args.limit)
        if not rows:
            print("No execution trace rollups.")
            return
        print("Execution trace rollups:")
        for row in rows:
            print(
                f"- {row['bucket_date']} {row['command_path']} status={row['status']} "
                f"count={row['trace_count']} duration_ms={row['total_duration_ms']}"
            )
        return
    raise SystemExit("Unknown trace command.")


def cmd_autonomy(args: argparse.Namespace) -> None:
    action = getattr(args, "autonomy_action", "")
    conn = get_connection()
    if action == "eval":
        result = autonomy.evaluate_command_decisions(level=args.level)
        summary = result["summary"]
        run_id = 0
        if not args.no_record:
            run_id = autonomy.record_command_decision_eval(conn, result)
        print("Autonomy eval:")
        print(f"- fixtures: {summary['total']}")
        print(f"- passed: {summary['passed']} failed={summary['failed']} accuracy={summary['accuracy']:.2%}")
        print(f"- calibration: {summary['calibration']}")
        if run_id:
            print(f"- recorded_eval_run: #{run_id}")
        failures = [case for case in result["cases"] if not case["passed"]]
        if failures:
            print("Failures:")
            for case in failures[:10]:
                print(
                    f"- {case['fixture_id']}: command={case['command']} "
                    f"expected={case['expected_decision']} actual={case['actual_decision']}"
                )
        return
    if action == "feedback":
        try:
            feedback_id = autonomy.record_command_decision_feedback(
                conn,
                trace_id=args.trace,
                expected_decision=args.expected_decision,
                note=args.note or "",
            )
        except ValueError as exc:
            print(f"Autonomy feedback failed: {exc}")
            raise SystemExit(1) from exc
        print(f"Autonomy feedback recorded: #{feedback_id}")
        print("Privacy: note text was hashed; raw command arguments were not stored.")
        return
    raise SystemExit("Unknown autonomy command.")


def cmd_smart_help(args: argparse.Namespace) -> None:
    inventory = router.command_inventory()
    tier = "workflow" if args.tier == "workflows" else args.tier
    if tier == "all":
        tiers = ["daily", "workflow", "expert", "diagnostic"]
    else:
        tiers = [tier]
    print("MYOS smart command surface")
    print("Primary: myos chat | myos voice | myos autopilot --factory | myos do \"...\" | myos approve --list")
    for name in tiers:
        commands = inventory.get(name, [])
        print(f"\n{name.title()} commands:")
        for command in commands:
            print(f"- myos {command}")


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
    factory_step: dict[str, object] = {}
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
        if getattr(args, "factory", False):
            routed_factory = router.choose_autopilot_workflow(signals)
            requested_pack = getattr(args, "factory_pack", "auto")
            workflow_pack = routed_factory["workflow_pack"] if requested_pack == "auto" else requested_pack
            factory_step = factory.proactive_step(
                conn,
                mode=getattr(args, "factory_mode", "review_first"),
                workflow_pack=workflow_pack,
            )
            factory_step["router_intent"] = routed_factory["intent"]
            factory_step["router_reason"] = routed_factory["reason"]
            factory_step["workflow_pack"] = workflow_pack
            conn.commit()
        safe_actions = _execute_safe_autopilot_actions(conn, args.safe_action_limit, created_task_ids)
        approvals_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_actions WHERE status='proposed' AND requires_approval=1"
        ).fetchone()["c"]
        summary = (
            f"synced={synced}, signals={signals_detected}, tasks_created={tasks_created}, "
            f"safe_actions={safe_actions}, approvals_pending={approvals_pending}, "
            f"watched_files={watched_files}, watch_suggestions={watch_suggestions}, "
            f"factory={factory_step.get('action', 'off') if factory_step else 'off'}"
            f"/{factory_step.get('workflow_pack', 'none') if factory_step else 'none'}"
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
            rollback = payload.get("rollback_note") or payload.get("rollback")
            if rollback:
                print(f"  rollback: {rollback}")
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


def cmd_intent(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "intent_action", "")
    if action == "create":
        intent_id = intents.create_intent(
            conn,
            objective=args.objective,
            context=args.context,
            constraints=args.constraint,
            success_criteria=args.success,
            priority=args.priority,
        )
        conn.commit()
        intent = intents.get_intent(conn, intent_id)
        objective = intent["objective"] if intent else args.objective
        print(f"Created intent #{intent_id}: {objective}")
        return

    if action == "list":
        rows = intents.list_intents(conn, status=args.status, limit=args.limit)
        if not rows:
            print("No intents found.")
            return
        print("Intents:")
        for row in rows:
            success = f" success={row['success_criteria']}" if row["success_criteria"] else ""
            print(
                f"- #{row['id']} status={row['status']} priority={row['priority']}"
                f" objective={row['objective']}{success}"
            )
        return

    if action == "show":
        intent = intents.get_intent(conn, args.id)
        if intent is None:
            print(f"Intent #{args.id} not found.")
            raise SystemExit(1)
        print(f"Intent #{intent['id']}")
        print(f"Status: {intent['status']}")
        print(f"Priority: {intent['priority']}")
        print(f"Objective: {intent['objective']}")
        if intent.get("context"):
            print(f"Context: {intent['context']}")
        if intent.get("success_criteria"):
            print(f"Success: {intent['success_criteria']}")
        if intent.get("constraints"):
            print("Constraints:")
            for constraint in intent["constraints"]:
                print(f"- {constraint}")
        print("Evidence:")
        for evidence in intent["evidence"]:
            source_id = f":{evidence['source_id']}" if evidence["source_id"] is not None else ""
            summary = f" summary={evidence['summary']}" if evidence["summary"] else ""
            print(
                f"- #{evidence['id']} source={evidence['source_type']}{source_id}"
                f" confidence={evidence['confidence']:.2f}{summary}"
            )
            print(f"  {evidence['content']}")
        if not intent["evidence"]:
            print("- none")
        return

    if action == "evidence" and getattr(args, "evidence_action", "") == "add":
        try:
            evidence_id = intents.add_evidence(
                conn,
                intent_id=args.id,
                content=args.text,
                source_type=args.source_type,
                source_id=args.source_id,
                summary=args.summary,
                confidence=args.confidence,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Added evidence #{evidence_id} to intent #{args.id}.")
        return

    raise SystemExit("Unknown intent command.")


def cmd_plan(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "plan_action", "")
    if action == "create":
        try:
            plan_id = plans.create_plan(
                conn,
                intent_id=args.intent,
                title=args.title,
                assumptions=args.assumption,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        plan = plans.get_plan(conn, plan_id)
        print(f"Created plan #{plan_id} for intent #{args.intent}: {plan['title'] if plan else args.title}")
        return

    if action == "show":
        plan = plans.get_plan(conn, args.id)
        if plan is None:
            print(f"Plan #{args.id} not found.")
            raise SystemExit(1)
        print(f"Plan #{plan['id']} intent={plan['intent_id']} status={plan['status']}")
        print(f"Title: {plan['title']}")
        if plan.get("summary"):
            print(f"Summary: {plan['summary']}")
        if plan.get("assumptions"):
            print("Assumptions:")
            for assumption in plan["assumptions"]:
                print(f"- {assumption}")
        print("Steps:")
        for step in plan["steps"]:
            print(f"{step['step_index']}. {step['description']} [{step['status']}]")
            if step["validation"]:
                print(f"   validation: {step['validation']}")
        print("Risks:")
        for risk in plan["risks"]:
            print(f"- [{risk['severity']}] {risk['risk']} -> {risk['mitigation']}")
        print("Validations:")
        for validation in plan["validations"]:
            command = f" command={validation['command']}" if validation["command"] else ""
            print(f"- {validation['check_name']} [{validation['status']}]{command}")
        return

    raise SystemExit("Unknown plan command.")


def cmd_evidence(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "evidence_action", "")
    if action == "attach":
        try:
            evidence_id = plans.attach_retrieval_run_evidence(
                conn,
                intent_id=args.intent,
                retrieval_run_id=args.retrieval_run,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Attached retrieval run #{args.retrieval_run} as evidence #{evidence_id} to intent #{args.intent}.")
        return
    if action == "sync-external":
        intent = intents.get_intent(conn, args.intent)
        if intent is None:
            print(f"Intent #{args.intent} not found.")
            raise SystemExit(1)
        objective_terms = {
            term.strip(".,:;!?()[]{}").lower()
            for term in str(intent["objective"]).split()
            if len(term.strip(".,:;!?()[]{}")) >= 4
        }
        connector_clause = "" if args.connector == "all" else "WHERE connector = ?"
        params: tuple[object, ...] = (int(args.limit),) if args.connector == "all" else (args.connector, int(args.limit))
        rows = conn.execute(
            f"""
            SELECT id, connector, external_id, item_type, title, body, url
            FROM external_items
            {connector_clause}
            ORDER BY fetched_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        attached = 0
        for row in rows:
            haystack = f"{row['title']} {row['body'] or ''}".lower()
            if objective_terms and not any(term in haystack for term in objective_terms):
                continue
            source_id = str(row["id"])
            exists = conn.execute(
                """
                SELECT 1 FROM intent_evidence
                WHERE intent_id = ? AND source_type = 'external_item' AND source_id = ?
                LIMIT 1
                """,
                (int(args.intent), source_id),
            ).fetchone()
            if exists:
                continue
            content_parts = [
                f"{row['connector']}:{row['external_id']} ({row['item_type']})",
                row["title"],
            ]
            if row["body"]:
                content_parts.append(str(row["body"])[:1000])
            if row["url"]:
                content_parts.append(str(row["url"]))
            intents.add_evidence(
                conn,
                intent_id=args.intent,
                content="\n".join(content_parts),
                source_type="external_item",
                source_id=source_id,
                summary=f"{row['connector']} {row['item_type']}: {row['title']}",
                confidence=0.75,
            )
            attached += 1
        append_event(
            conn,
            "connector_evidence_synced",
            "intent",
            int(args.intent),
            json.dumps({"connector": args.connector, "attached": attached}, ensure_ascii=True),
        )
        conn.commit()
        print(f"Attached {attached} external evidence item(s) to intent #{args.intent}.")
        return
    raise SystemExit("Unknown evidence command.")


def cmd_review_packet(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        packet_id = plans.create_review_packet(
            conn,
            plan_id=args.plan,
            retrieval_run_id=args.retrieval_run,
        )
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from exc
    conn.commit()
    packet = plans.get_review_packet(conn, packet_id)
    print(f"Review packet #{packet_id} for plan #{args.plan}")
    if packet:
        body = packet["packet"]
        print(f"Summary: {packet['summary']}")
        print(f"Intent: #{body['intent']['id']} {body['intent']['objective']}")
        print("Steps:")
        for step in body["plan"]["steps"]:
            print(f"- {step['description']}")
        print("Risks:")
        for risk in body["plan"]["risks"]:
            print(f"- {risk['risk']} -> {risk['mitigation']}")
        print("Evidence:")
        if body["evidence"]:
            for evidence in body["evidence"]:
                source_id = f":{evidence['source_id']}" if evidence["source_id"] is not None else ""
                print(f"- {evidence['source_type']}{source_id}: {evidence['summary'] or evidence['content'][:80]}")
        else:
            print("- none")
        if body["retrieval_sources"]:
            print("Retrieval sources:")
            for source in body["retrieval_sources"]:
                print(f"- {source['citation']} score={source['score']:.3f} reason={source['reason']}")
        print(f"Approval required: {body['approval_required']}")
        print(f"Rollback: {body['rollback_note']}")


def cmd_execution_receipt(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "receipt_action", "list") or "list"
    if action == "show":
        if not args.id:
            print("--id is required for receipt show.")
            raise SystemExit(1)
        row = conn.execute(
            """
            SELECT r.*, a.title
            FROM action_execution_receipts r
            JOIN agent_actions a ON a.id = r.agent_action_id
            WHERE r.id = ?
            """,
            (int(args.id),),
        ).fetchone()
        if not row:
            print(f"Execution receipt #{args.id} not found.")
            raise SystemExit(1)
        print(f"Execution receipt #{row['id']} action=#{row['agent_action_id']} status={row['final_status']}")
        print(f"Type: {row['action_type']}")
        print(f"Title: {row['title']}")
        print(f"Approved: {bool(row['approved'])}")
        print(f"Follow-up required: {bool(row['follow_up_required'])}")
        if row["follow_up_inbox_id"]:
            print(f"Follow-up inbox item: #{row['follow_up_inbox_id']}")
        try:
            request = json.loads(row["request_json"] or "{}")
        except (TypeError, ValueError):
            request = {}
        payload = request.get("payload") if isinstance(request, dict) else {}
        if isinstance(payload, dict):
            print(f"Target: {_provider_target_summary(payload)}")
        outbox = conn.execute(
            """
            SELECT id, provider, target_type, target_ref, status
            FROM action_outbox
            WHERE agent_action_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(row["agent_action_id"]),),
        ).fetchone()
        if outbox:
            print(
                f"Outbox: #{outbox['id']} provider={outbox['provider']} "
                f"target={outbox['target_type']}:{outbox['target_ref']} status={outbox['status']}"
            )
        print(f"Result: {row['result']}")
        print(f"Rollback: {row['rollback_note']}")
        return

    rows = conn.execute(
        """
        SELECT id, agent_action_id, action_type, final_status, approved, follow_up_required, follow_up_inbox_id, created_at
        FROM action_execution_receipts
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (int(args.limit),),
    ).fetchall()
    if not rows:
        print("No execution receipts found.")
        return
    print("Execution receipts:")
    for row in rows:
        follow_up = " follow_up_required" if row["follow_up_required"] else ""
        follow_up_id = f" follow_up=#{row['follow_up_inbox_id']}" if row["follow_up_inbox_id"] else ""
        print(
            f"- #{row['id']} action=#{row['agent_action_id']} [{row['action_type']}] "
            f"status={row['final_status']} approved={bool(row['approved'])}{follow_up}{follow_up_id}"
        )


def cmd_agent_run(args: argparse.Namespace) -> None:
    conn = get_connection()
    intent = intents.get_intent(conn, args.intent)
    if intent is None:
        print(f"Intent #{args.intent} not found.")
        raise SystemExit(1)
    plan = plans.get_plan(conn, args.plan) if args.plan else None
    if args.plan and plan is None:
        print(f"Plan #{args.plan} not found.")
        raise SystemExit(1)
    role = args.role
    objective = f"{role}: {intent['objective']}"
    conn.execute(
        """
        INSERT INTO agent_tasks (objective, context, constraints_json, priority, status)
        VALUES (?, ?, ?, ?, 'open')
        """,
        (
            objective,
            intent.get("context") or "",
            json.dumps(intent.get("constraints", []), ensure_ascii=True),
            int(intent.get("priority") or 2),
        ),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    role_packet = {
        "role": role,
        "intent_id": int(args.intent),
        "plan_id": int(args.plan) if args.plan else None,
        "retrieval_run_id": int(args.retrieval_run) if args.retrieval_run else None,
        "objective": intent["objective"],
        "responsibilities": {
            "planner": "Turn intent into a bounded plan with assumptions and validation gates.",
            "researcher": "Gather cited evidence and identify missing context.",
            "executor": "Draft only policy-allowed actions and never bypass approval.",
            "reviewer": "Check plan completeness, evidence, risks, and rollback notes.",
            "critic": "Find failure modes, stale assumptions, and unsafe actions.",
            "summarizer": "Produce a concise status summary with citations and next steps.",
        }[role],
        "approval_gate": role in {"reviewer", "critic", "executor"},
    }
    summary = (
        f"{role} run for intent #{args.intent}"
        + (f" plan #{args.plan}" if args.plan else "")
        + (f" retrieval_run #{args.retrieval_run}" if args.retrieval_run else "")
    )
    conn.execute(
        """
        INSERT INTO agent_runs (agent_task_id, agent_name, provider, status, plan_json, summary, finished_at)
        VALUES (?, ?, 'local', 'completed', ?, ?, CURRENT_TIMESTAMP)
        """,
        (task_id, role, json.dumps(role_packet, ensure_ascii=True), summary),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    append_event(
        conn,
        "agent_role_run_completed",
        "agent_run",
        run_id,
        json.dumps({"intent_id": int(args.intent), "role": role, "plan_id": args.plan}, ensure_ascii=True),
    )
    conn.commit()
    print(f"Agent run #{run_id} [{role}] for intent #{args.intent}")
    print(f"task: #{task_id}")
    print(f"summary: {summary}")
    print(f"approval_gate: {role_packet['approval_gate']}")


def cmd_factory(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "factory_action", "")

    if action == "start":
        autonomy_decision = _command_autonomy_decision(conn, "factory", requested_mode=args.mode)
        _print_autonomy_decision(autonomy_decision)
        _print_recommendations(
            autonomy.recommend_next_steps(
                autonomy_decision,
                command="factory",
                workflow_pack=args.pack,
            )
        )
        if autonomy_decision["decision"] == autonomy.BLOCKED:
            raise SystemExit(1)
        try:
            result = factory.start_review_first_run(
                conn,
                intent_id=args.intent,
                mode=args.mode,
                workflow_pack=args.pack,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Factory run #{result['id']} for intent #{result['intent_id']} status={result['status']}")
        print(f"mode={args.mode} pack={args.pack} plan=#{result['plan_id']}")
        if result["retrieval_run_id"] is not None:
            print(f"retrieval_run=#{result['retrieval_run_id']}")
        print(f"review_packet=#{result['review_packet_id']}")
        print("agent_runs=" + ",".join(f"#{run_id}" for run_id in result["agent_run_ids"]))
        print(f"stopped_before_execution={args.mode == 'review_first'}")
        _print_recommendations(
            autonomy.recommend_next_steps(
                autonomy_decision,
                command="factory",
                workflow_pack=args.pack,
                factory_run_id=result["id"],
            )
        )
        return

    if action == "status":
        run = factory.get_factory_run(conn, args.id)
        if run is None:
            print(f"Factory run #{args.id} not found.")
            raise SystemExit(1)
        print(
            f"Factory run #{run['id']} intent=#{run['intent_id']} plan=#{run['plan_id']} "
            f"mode={run['mode']} pack={run['workflow_pack']} status={run['status']}"
        )
        if run.get("summary"):
            print(f"Summary: {run['summary']}")
        if run.get("outcome"):
            print(f"Outcome: {run['outcome']} notes={run.get('outcome_notes') or ''}")
        print("Stages:")
        for stage in run["stages"]:
            agent = f" agent_run=#{stage['agent_run_id']}" if stage["agent_run_id"] else ""
            role = f" role={stage['role']}" if stage["role"] else ""
            print(f"- {stage['stage_name']} status={stage['status']}{role}{agent}")
        print("Artifacts:")
        for artifact in run["artifacts"]:
            label = f" {artifact['label']}" if artifact["label"] else ""
            print(f"- {artifact['artifact_type']}#{artifact['artifact_id']}{label}")
        return

    if action == "run-stage":
        try:
            factory.record_stage(
                conn,
                factory_run_id=args.id,
                stage_name=args.stage,
                status=args.status,
                note=args.note,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Factory run #{args.id} stage {args.stage} -> {args.status}")
        return

    if action == "continue":
        run = factory.get_factory_run(conn, args.id)
        if run is None:
            print(f"Factory run #{args.id} not found.")
            raise SystemExit(1)
        next_stage = next((s for s in run["stages"] if s["status"] in {"pending", "waiting"}), None)
        if next_stage is None:
            print(f"Factory run #{args.id} has no pending stages.")
            return
        if next_stage["stage_name"] == "execution":
            try:
                result = factory.advance_execution(conn, args.id)
            except ValueError as exc:
                print(str(exc))
                raise SystemExit(1) from exc
            conn.commit()
            print(
                f"Factory run #{args.id} execution advanced: "
                f"actions={result['actions']} executed={result['executed']} pending={result['pending']} blocked={result['blocked']}"
            )
            return
        if next_stage["stage_name"] == "learning":
            print(f"Factory run #{args.id} is waiting for outcome before learning.")
            return
        factory.record_stage(conn, factory_run_id=args.id, stage_name=next_stage["stage_name"], status="completed")
        conn.commit()
        print(f"Factory run #{args.id} continued stage {next_stage['stage_name']}.")
        return

    if action == "review":
        run = factory.get_factory_run(conn, args.id)
        if run is None:
            print(f"Factory run #{args.id} not found.")
            raise SystemExit(1)
        post_review = {"execution", "learning"}
        gaps = [
            s["stage_name"]
            for s in run["stages"]
            if s["status"] in {"pending", "blocked"} and s["stage_name"] not in post_review
        ]
        packet = next((a for a in run["artifacts"] if a["artifact_type"] == "review_packet"), None)
        print(f"Factory review #{args.id}: {'ready_for_approval' if not gaps and packet else 'needs_attention'}")
        print(f"review_packet=#{packet['artifact_id'] if packet else 'missing'}")
        if gaps:
            print("Open gaps: " + ", ".join(gaps))
        print("Execution remains approval-gated.")
        return

    if action == "approve":
        run = factory.get_factory_run(conn, args.id)
        if run is None:
            print(f"Factory run #{args.id} not found.")
            raise SystemExit(1)
        conn.execute(
            "UPDATE factory_runs SET status=? WHERE id=?",
            ("approved_for_execution" if args.execute else "approved", int(args.id)),
        )
        factory.record_stage(conn, factory_run_id=args.id, stage_name="approval", status="completed", note="factory approval recorded")
        if args.execute:
            try:
                execution = factory.advance_execution(conn, args.id, approve=True)
            except ValueError as exc:
                print(str(exc))
                raise SystemExit(1) from exc
        conn.commit()
        print(f"Factory run #{args.id} approved.")
        if args.execute:
            print(
                f"Execution advanced: actions={execution['actions']} executed={execution['executed']} "
                f"pending={execution['pending']} blocked={execution['blocked']}"
            )
        return

    if action == "policy":
        policy_action = getattr(args, "policy_action", "")
        if policy_action == "set":
            try:
                policy_id = factory.set_policy(
                    conn,
                    allowed_mode=args.mode,
                    scope_type=args.scope_type,
                    scope_id=args.scope_id,
                    connector=args.connector,
                    action_type=args.action_type,
                )
            except ValueError as exc:
                print(str(exc))
                raise SystemExit(1) from exc
            conn.commit()
            scope = f"{args.scope_type}:{args.scope_id}" if args.scope_id else args.scope_type
            print(f"Factory policy #{policy_id} {scope} connector={args.connector or 'all'} action={args.action_type or 'all'} mode={args.mode}")
            return
        rows = conn.execute(
            """
            SELECT id, scope_type, scope_id, connector, action_type, allowed_mode, status
            FROM factory_policies
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(args.limit),),
        ).fetchall()
        if not rows:
            print("No factory policies configured.")
            return
        print("Factory policies:")
        for row in rows:
            scope = f"{row['scope_type']}:{row['scope_id']}" if row["scope_id"] else row["scope_type"]
            connector = row["connector"] or "all"
            action_type = row["action_type"] or "all"
            print(f"- #{row['id']} {scope} connector={connector} action={action_type} mode={row['allowed_mode']} status={row['status']}")
        return

    if action == "learn":
        try:
            learning_id = factory.learn(conn, factory_run_id=args.id, outcome=args.outcome, notes=args.notes)
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Factory learning #{learning_id} recorded for run #{args.id}: {args.outcome}")
        return

    if action == "retrospective":
        run = factory.get_factory_run(conn, args.id)
        if run is None:
            print(f"Factory run #{args.id} not found.")
            raise SystemExit(1)
        retro = factory.latest_retrospective(conn, args.id)
        if retro is None:
            print(f"No retrospective recorded for factory run #{args.id}.")
            return
        body = retro["retrospective"]
        print(f"Factory retrospective #{args.id}: outcome={retro['outcome']}")
        print(f"notes={retro['notes'] or ''}")
        print(f"stages={body.get('stage_count', 0)} artifacts={body.get('artifact_count', 0)}")
        receipts = body.get("recent_receipts") or []
        print(f"recent_receipts={len(receipts)}")
        return

    if action == "insights":
        insights = factory.learning_insights(
            conn,
            intent_id=args.intent,
            workflow_pack=args.pack,
            limit=args.limit,
        )
        scope = f"intent=#{args.intent}" if args.intent else "all intents"
        pack = args.pack or "all packs"
        print(f"Factory insights ({scope}, {pack}): runs={insights['count']}")
        print(f"outcomes={json.dumps(insights['outcomes'], ensure_ascii=True, sort_keys=True)}")
        print(f"blockers={json.dumps(insights['blockers'], ensure_ascii=True, sort_keys=True)}")
        print(f"useful_sources={json.dumps(insights['useful_sources'], ensure_ascii=True, sort_keys=True)}")
        if insights["notes"]:
            print("Notes:")
            for note in insights["notes"]:
                print(f"- {note}")
        return

    raise SystemExit("Unknown factory command.")


def cmd_entity(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "entity_action", "")
    if action == "extract":
        recorded = entities.record_entities(
            conn,
            args.text,
            source_type=args.source_type,
            source_id=args.source_id,
        )
        conn.commit()
        if not recorded:
            print("No deterministic entities found.")
            return
        print(f"Recorded {len(recorded)} entities:")
        for entity in recorded:
            aliases = ", ".join(entity["aliases"])
            print(
                f"- #{entity['id']} [{entity['entity_type']}] {entity['canonical_name']} "
                f"confidence={entity['confidence']:.2f} aliases={aliases}"
            )
        return

    if action == "list":
        rows = entities.list_entities(conn, entity_type=args.type, limit=args.limit)
        if not rows:
            print("No entities found.")
            return
        print("Entities:")
        for row in rows:
            aliases = ", ".join(row["aliases"]) if row["aliases"] else "none"
            print(f"- #{row['id']} [{row['entity_type']}] {row['canonical_name']} aliases={aliases}")
        return

    raise SystemExit("Unknown entity command.")


def cmd_relationship(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "relationship_action", "")
    if action == "extract":
        recorded = relationships.record_relationships(
            conn,
            args.text,
            source_type=args.source_type,
            source_id=args.source_id,
        )
        conn.commit()
        if not recorded:
            print("No deterministic relationships found.")
            return
        print(f"Recorded {len(recorded)} relationships:")
        for rel in recorded:
            src = rel["from_entity"]["canonical_name"]
            dst = rel["to_entity"]["canonical_name"]
            print(
                f"- #{rel['id']} {src} -[{rel['relation_type']}]-> {dst} "
                f"confidence={rel['confidence']:.2f}"
            )
        return

    if action == "list":
        rows = relationships.list_relationships(conn, relation_type=args.type, limit=args.limit)
        if not rows:
            print("No relationships found.")
            return
        print("Relationships:")
        for row in rows:
            source = f" source={row['source_type']}:{row['source_id']}" if row["source_type"] else ""
            print(
                f"- #{row['id']} {row['from_name']} -[{row['relation_type']}]-> {row['to_name']}"
                f" confidence={row['confidence']:.2f}{source}"
            )
        return

    raise SystemExit("Unknown relationship command.")


def cmd_claim(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "claim_action", "")
    if action == "extract":
        recorded = claims.record_claims(
            conn,
            args.text,
            source_type=args.source_type,
            source_id=args.source_id,
        )
        conn.commit()
        if not recorded:
            print("No high-confidence claims found.")
            return
        print(f"Recorded {len(recorded)} claim(s):")
        for claim in recorded:
            print(f"- #{claim['id']} ({claim['confidence']:.2f}) {claim['claim_text']}")
        return

    if action == "list":
        rows = claims.list_claims(conn, source_type=args.source_type, limit=args.limit)
        if not rows:
            print("No claims recorded.")
            return
        print("Claims:")
        for row in rows:
            source_id = f":{row['source_id']}" if row["source_id"] else ""
            print(
                f"- #{row['id']} source={row['source_type']}{source_id} "
                f"confidence={row['confidence']:.2f} {row['claim_text']}"
            )
        return

    raise SystemExit("Unknown claim command.")


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

    do = sub.add_parser("do", help="Route a natural-language request to the right MYOS workflow.")
    do.add_argument("text", help="What you want MYOS to do.")
    do.set_defaults(func=cmd_do)

    smart_help = sub.add_parser("help", help="Show simplified daily/workflow/expert command tiers.")
    smart_help.add_argument("tier", nargs="?", choices=["daily", "workflow", "workflows", "expert", "diagnostic", "all"], default="daily")
    smart_help.set_defaults(func=cmd_smart_help)

    model = sub.add_parser("model", help="Manage optional tiny local models for MYOS routing.")
    model_sub = model.add_subparsers(dest="model_action", required=True)
    model_recommend = model_sub.add_parser("recommend", help="Recommend a small local model for a purpose.")
    model_recommend.add_argument("--purpose", choices=["router"], default="router")
    model_recommend.set_defaults(func=cmd_model)
    model_setup_parser = model_sub.add_parser("setup", help="Plan or apply tiny local model setup.")
    model_setup_parser.add_argument("--router", action="store_true", help="Configure the router intent model.")
    model_setup_parser.add_argument("--runtime", choices=["auto", "ollama", "llama-cpp", "command"], default="auto")
    model_setup_parser.add_argument("--model", choices=list(model_setup.ROUTER_MODELS), default=model_setup.DEFAULT_ROUTER_MODEL)
    model_setup_parser.add_argument("--command", default="", help="Custom MYOS_ROUTER_COMMAND for runtime=command.")
    model_setup_parser.add_argument("--apply", action="store_true", help="Pull/download and write local wrapper files.")
    model_setup_parser.set_defaults(func=cmd_model)
    model_status = model_sub.add_parser("status", help="Show router model readiness.")
    model_status.set_defaults(func=cmd_model)

    router_parser = sub.add_parser("router", help="Evaluate and improve smart routing quality.")
    router_sub = router_parser.add_subparsers(dest="router_action", required=True)
    router_eval = router_sub.add_parser("eval", help="Evaluate route fixtures and calibration.")
    router_eval.add_argument("--fixture", default="", help="Optional route eval fixture JSON path.")
    router_eval.add_argument("--model-shadow", action="store_true", help="Compare local model decisions when configured.")
    router_eval.add_argument("--no-record", action="store_true", help="Do not persist eval metadata.")
    router_eval.set_defaults(func=cmd_router)
    router_feedback = router_sub.add_parser("feedback", help="Record privacy-safe route correction metadata.")
    router_feedback.add_argument("--event", type=int, required=True, help="smart_route event_log id.")
    router_feedback.add_argument("--expected-intent", choices=list(router.ROUTABLE_INTENTS), required=True)
    router_feedback.add_argument("--note", default="", help="Optional note; stored as hash and length only.")
    router_feedback.set_defaults(func=cmd_router)
    router_overrides = router_sub.add_parser("overrides", help="List active exact-hash route overrides.")
    router_overrides.add_argument("--limit", type=int, default=20)
    router_overrides.set_defaults(func=cmd_router)
    router_commands = router_sub.add_parser("commands", help="List router-visible MYOS command metadata.")
    router_commands.add_argument("--tier", choices=list(command_registry.TIERS), default="")
    router_commands.add_argument("--safety", choices=list(command_registry.SAFETY_LEVELS), default="")
    router_commands.add_argument("--intent", choices=list(router.ROUTABLE_INTENTS), default="")
    router_commands.add_argument("--limit", type=int, default=80)
    router_commands.set_defaults(func=cmd_router)

    trace = sub.add_parser("trace", help="Inspect lightweight command and agent execution traces.")
    trace_sub = trace.add_subparsers(dest="trace_action", required=True)
    trace_list = trace_sub.add_parser("list", help="List recent bounded execution traces.")
    trace_list.add_argument("--limit", type=int, default=20)
    trace_list.add_argument("--status", default="")
    trace_list.add_argument("--command", dest="command_filter", default="")
    trace_list.set_defaults(func=cmd_trace)
    trace_cleanup = trace_sub.add_parser("cleanup", help="Roll up and delete old detailed traces.")
    trace_cleanup.add_argument("--retention-days", type=int, default=observability.DEFAULT_RETENTION_DAYS)
    trace_cleanup.add_argument("--max-rows", type=int, default=observability.DEFAULT_MAX_ROWS)
    trace_cleanup.set_defaults(func=cmd_trace)
    trace_rollups = trace_sub.add_parser("rollups", help="Show retained aggregate trace counts.")
    trace_rollups.add_argument("--limit", type=int, default=20)
    trace_rollups.set_defaults(func=cmd_trace)

    autonomy_parser = sub.add_parser("autonomy", help="Evaluate and calibrate autonomy decision policy.")
    autonomy_sub = autonomy_parser.add_subparsers(dest="autonomy_action", required=True)
    autonomy_eval = autonomy_sub.add_parser("eval", help="Evaluate local autonomy decision fixtures.")
    autonomy_eval.add_argument("--level", choices=list(autonomy.LEVELS), default=autonomy.DEFAULT_LEVEL)
    autonomy_eval.add_argument("--no-record", action="store_true", help="Do not persist eval metadata.")
    autonomy_eval.set_defaults(func=cmd_autonomy)
    autonomy_feedback = autonomy_sub.add_parser("feedback", help="Record privacy-safe autonomy decision feedback.")
    autonomy_feedback.add_argument("--trace", type=int, required=True, help="execution_traces id.")
    autonomy_feedback.add_argument("--expected-decision", choices=list(autonomy.DECISIONS), required=True)
    autonomy_feedback.add_argument("--note", default="", help="Optional note; stored as hash and length only.")
    autonomy_feedback.set_defaults(func=cmd_autonomy)

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
    context.add_argument("--graph", action="store_true", help="Use SQLite GraphRAG retrieval trace.")
    context.add_argument("--graph-hops", type=int, default=1)
    context.set_defaults(func=cmd_context)

    retrieval_run = sub.add_parser("retrieval-run", help="Inspect persisted retrieval traces.")
    retrieval_run.add_argument("retrieval_run_action", nargs="?", choices=["list", "show"], default="list")
    retrieval_run.add_argument("--id", type=int)
    retrieval_run.add_argument("--limit", type=int, default=10)
    retrieval_run.set_defaults(func=cmd_retrieval_run)

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
    setup_live.add_argument("--router-model", action="store_true", help="Also configure the tiny local router model.")
    setup_live.add_argument("--router-runtime", choices=["auto", "ollama", "llama-cpp", "command"], default="auto")
    setup_live.add_argument("--router-model-name", choices=list(model_setup.ROUTER_MODELS), default=model_setup.DEFAULT_ROUTER_MODEL)
    setup_live.set_defaults(func=cmd_setup_live)

    onboard = sub.add_parser("onboard", help="Show connector onboarding diagnostics.")
    onboard.set_defaults(func=cmd_onboard)

    doctor = sub.add_parser("doctor", help="Show local system and connector health.")
    doctor.add_argument("--strict", action="store_true", help="Exit non-zero if core local checks fail.")
    doctor.set_defaults(func=cmd_doctor)

    backup = sub.add_parser("backup", help="Create a verified SQLite database backup.")
    backup.add_argument("--output", default="", help="Destination .db file. Defaults to data/backups timestamp.")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="Restore the SQLite database from a backup.")
    restore.add_argument("--from", dest="source", required=True, help="Backup .db file to restore from.")
    restore.set_defaults(func=cmd_restore)

    migrations = sub.add_parser("migrations", help="Inspect and verify schema migration health.")
    migrations.add_argument("migrations_action", nargs="?", choices=["verify", "list"], default="verify")
    migrations.add_argument("--strict", action="store_true", help="Exit non-zero if verification fails.")
    migrations.set_defaults(func=cmd_migrations)

    dependency_check = sub.add_parser("dependency-check", help="Check local dependency and license hygiene.")
    dependency_check.add_argument("--strict", action="store_true")
    dependency_check.set_defaults(func=cmd_dependency_check)

    perf = sub.add_parser("performance-baseline", help="Measure retrieval and readiness query timing.")
    perf.add_argument("--query", default="daily priorities risks approvals")
    perf.add_argument("--limit", type=int, default=5)
    perf.set_defaults(func=cmd_performance_baseline)

    release_check = sub.add_parser("release-check", help="Run local release readiness checks.")
    release_check.add_argument("--strict", action="store_true")
    release_check.add_argument("--verbose", action="store_true")
    release_check.set_defaults(func=cmd_release_check)

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
    why.add_argument("--graph", action="store_true", help="Include graph-related evidence and path explanations.")
    why.add_argument("--limit", type=int, default=5)
    why.add_argument("--graph-hops", type=int, default=1)
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

    entity = sub.add_parser("entity", help="Extract and list deterministic graph entities.")
    entity_sub = entity.add_subparsers(dest="entity_action", required=True)
    entity_extract = entity_sub.add_parser("extract", help="Extract entities from text and persist them.")
    entity_extract.add_argument("--text", required=True)
    entity_extract.add_argument("--source-type", default="note")
    entity_extract.add_argument("--source-id")
    entity_extract.set_defaults(func=cmd_entity)
    entity_list = entity_sub.add_parser("list", help="List extracted entities.")
    entity_list.add_argument("--type", default="", help="Optional entity type filter.")
    entity_list.add_argument("--limit", type=int, default=50)
    entity_list.set_defaults(func=cmd_entity)

    relationship = sub.add_parser("relationship", help="Extract and list typed entity relationships.")
    relationship_sub = relationship.add_subparsers(dest="relationship_action", required=True)
    relationship_extract = relationship_sub.add_parser("extract", help="Extract typed relationships from text.")
    relationship_extract.add_argument("--text", required=True)
    relationship_extract.add_argument("--source-type", default="note")
    relationship_extract.add_argument("--source-id")
    relationship_extract.set_defaults(func=cmd_relationship)
    relationship_list = relationship_sub.add_parser("list", help="List extracted relationships.")
    relationship_list.add_argument("--type", default="", help="Optional relation type filter.")
    relationship_list.add_argument("--limit", type=int, default=50)
    relationship_list.set_defaults(func=cmd_relationship)

    claim = sub.add_parser("claim", help="Extract and list deterministic claims.")
    claim_sub = claim.add_subparsers(dest="claim_action", required=True)
    claim_extract = claim_sub.add_parser("extract", help="Extract claims from text and persist them.")
    claim_extract.add_argument("--text", required=True)
    claim_extract.add_argument("--source-type", default="note")
    claim_extract.add_argument("--source-id")
    claim_extract.set_defaults(func=cmd_claim)
    claim_list = claim_sub.add_parser("list", help="List extracted claims.")
    claim_list.add_argument("--source-type", default="")
    claim_list.add_argument("--limit", type=int, default=50)
    claim_list.set_defaults(func=cmd_claim)

    intent = sub.add_parser("intent", help="Manage first-class assistant intents.")
    intent_sub = intent.add_subparsers(dest="intent_action", required=True)
    intent_create = intent_sub.add_parser("create", help="Create an intent objective.")
    intent_create.add_argument("objective")
    intent_create.add_argument("--context", default="")
    intent_create.add_argument("--constraint", action="append", default=[])
    intent_create.add_argument("--success", default="")
    intent_create.add_argument("--priority", type=int, default=2)
    intent_create.set_defaults(func=cmd_intent)
    intent_list = intent_sub.add_parser("list", help="List intents.")
    intent_list.add_argument("--status", default="open", help="Status filter, or 'all'.")
    intent_list.add_argument("--limit", type=int, default=20)
    intent_list.set_defaults(func=cmd_intent)
    intent_show = intent_sub.add_parser("show", help="Show one intent with evidence.")
    intent_show.add_argument("--id", type=int, required=True)
    intent_show.set_defaults(func=cmd_intent)
    intent_evidence = intent_sub.add_parser("evidence", help="Manage intent evidence.")
    evidence_sub = intent_evidence.add_subparsers(dest="evidence_action", required=True)
    evidence_add = evidence_sub.add_parser("add", help="Add evidence to an intent.")
    evidence_add.add_argument("--id", type=int, required=True)
    evidence_add.add_argument("--text", required=True)
    evidence_add.add_argument("--source-type", default="note")
    evidence_add.add_argument("--source-id")
    evidence_add.add_argument("--summary", default="")
    evidence_add.add_argument("--confidence", type=float, default=0.7)
    evidence_add.set_defaults(func=cmd_intent)

    plan = sub.add_parser("plan", help="Create and inspect intent-tied plans.")
    plan_sub = plan.add_subparsers(dest="plan_action", required=True)
    plan_create = plan_sub.add_parser("create", help="Create a draft plan for an intent.")
    plan_create.add_argument("--intent", type=int, required=True)
    plan_create.add_argument("--title", default="")
    plan_create.add_argument("--assumption", action="append", default=[])
    plan_create.set_defaults(func=cmd_plan)
    plan_show = plan_sub.add_parser("show", help="Show a draft plan with steps, risks, and validations.")
    plan_show.add_argument("--id", type=int, required=True)
    plan_show.set_defaults(func=cmd_plan)

    evidence = sub.add_parser("evidence", help="Attach evidence artifacts to intents.")
    evidence_sub = evidence.add_subparsers(dest="evidence_action", required=True)
    evidence_attach = evidence_sub.add_parser("attach", help="Attach a retrieval run to an intent.")
    evidence_attach.add_argument("--intent", type=int, required=True)
    evidence_attach.add_argument("--retrieval-run", type=int, required=True)
    evidence_attach.set_defaults(func=cmd_evidence)
    evidence_sync = evidence_sub.add_parser("sync-external", help="Map synced external items into intent evidence.")
    evidence_sync.add_argument("--intent", type=int, required=True)
    evidence_sync.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    evidence_sync.add_argument("--limit", type=int, default=50)
    evidence_sync.set_defaults(func=cmd_evidence)

    review_packet = sub.add_parser("review-packet", help="Build a review packet for a plan.")
    review_packet.add_argument("--plan", type=int, required=True)
    review_packet.add_argument("--retrieval-run", type=int)
    review_packet.set_defaults(func=cmd_review_packet)

    factory_parser = sub.add_parser("factory", help="Run review-first AI factory workflows.")
    factory_sub = factory_parser.add_subparsers(dest="factory_action", required=True)
    factory_start = factory_sub.add_parser("start", help="Start a traceable factory run for an intent.")
    factory_start.add_argument("--intent", type=int, required=True)
    factory_start.add_argument("--mode", choices=list(factory.MODES), default="review_first")
    factory_start.add_argument("--pack", choices=list(factory.WORKFLOW_PACKS), default="intent_execution")
    factory_start.set_defaults(func=cmd_factory)
    factory_status = factory_sub.add_parser("status", help="Show factory run stages and artifacts.")
    factory_status.add_argument("--id", type=int, required=True)
    factory_status.set_defaults(func=cmd_factory)
    factory_stage = factory_sub.add_parser("run-stage", help="Record or update one factory stage.")
    factory_stage.add_argument("--id", type=int, required=True)
    factory_stage.add_argument("--stage", choices=list(factory.STAGES), required=True)
    factory_stage.add_argument("--status", choices=["pending", "running", "completed", "waiting", "blocked", "failed"], default="completed")
    factory_stage.add_argument("--note", default="")
    factory_stage.set_defaults(func=cmd_factory)
    factory_continue = factory_sub.add_parser("continue", help="Continue the next non-execution pending stage.")
    factory_continue.add_argument("--id", type=int, required=True)
    factory_continue.set_defaults(func=cmd_factory)
    factory_review = factory_sub.add_parser("review", help="Review factory readiness before approval.")
    factory_review.add_argument("--id", type=int, required=True)
    factory_review.set_defaults(func=cmd_factory)
    factory_approve = factory_sub.add_parser("approve", help="Approve a factory run, optionally handing off to execution gates.")
    factory_approve.add_argument("--id", type=int, required=True)
    factory_approve.add_argument("--execute", action="store_true")
    factory_approve.set_defaults(func=cmd_factory)
    factory_policy = factory_sub.add_parser("policy", help="Configure or list factory autonomy policies.")
    factory_policy_sub = factory_policy.add_subparsers(dest="policy_action", required=True)
    factory_policy_set = factory_policy_sub.add_parser("set", help="Set an autonomy policy override.")
    factory_policy_set.add_argument("--mode", choices=list(factory.MODES), required=True)
    factory_policy_set.add_argument("--scope-type", choices=["global", "intent", "goal"], default="global")
    factory_policy_set.add_argument("--scope-id", default="")
    factory_policy_set.add_argument("--connector", default="")
    factory_policy_set.add_argument("--action-type", default="")
    factory_policy_set.set_defaults(func=cmd_factory)
    factory_policy_list = factory_policy_sub.add_parser("list", help="List factory autonomy policies.")
    factory_policy_list.add_argument("--limit", type=int, default=50)
    factory_policy_list.set_defaults(func=cmd_factory)
    factory_learn = factory_sub.add_parser("learn", help="Record the outcome of a factory run.")
    factory_learn.add_argument("--id", type=int, required=True)
    factory_learn.add_argument("--outcome", choices=["success", "partial", "failed"], required=True)
    factory_learn.add_argument("--notes", default="")
    factory_learn.set_defaults(func=cmd_factory)
    factory_retro = factory_sub.add_parser("retrospective", help="Show the latest factory retrospective.")
    factory_retro.add_argument("--id", type=int, required=True)
    factory_retro.set_defaults(func=cmd_factory)
    factory_insights = factory_sub.add_parser("insights", help="Show learned factory patterns.")
    factory_insights.add_argument("--intent", type=int)
    factory_insights.add_argument("--pack", choices=list(factory.WORKFLOW_PACKS), default="")
    factory_insights.add_argument("--limit", type=int, default=20)
    factory_insights.set_defaults(func=cmd_factory)

    delegate = sub.add_parser("delegate", help="Delegate an objective to the autonomous assistant core.")
    delegate.add_argument("objective", help="Outcome or task objective for the assistant.")
    delegate.add_argument("--context", default="", help="Additional context, transcript snippet, or constraints.")
    delegate.add_argument("--constraint", action="append", default=[], help="Repeatable constraint for this task.")
    delegate.add_argument("--mode", choices=["safe", "balanced", "aggressive"], default="safe")
    delegate.add_argument("--priority", type=int, default=2)
    delegate.add_argument("--max-actions", type=int, default=5)
    delegate.add_argument("--analogy-limit", type=int, default=5)
    delegate.add_argument("--to", default="", help="Harness an external agent CLI (copilot|command|claude) to execute this objective.")
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

    agent_run = sub.add_parser("agent-run", help="Run a local bounded agent role for an intent.")
    agent_run.add_argument("--intent", type=int, required=True)
    agent_run.add_argument("--role", choices=["planner", "researcher", "executor", "reviewer", "critic", "summarizer"], required=True)
    agent_run.add_argument("--plan", type=int)
    agent_run.add_argument("--retrieval-run", type=int)
    agent_run.set_defaults(func=cmd_agent_run)

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
    autopilot.add_argument("--factory", action="store_true", help="Start or continue one policy-aware factory run this cycle.")
    autopilot.add_argument("--factory-mode", choices=list(factory.MODES), default="review_first")
    autopilot.add_argument("--factory-pack", choices=["auto", *list(factory.WORKFLOW_PACKS)], default="auto")
    autopilot.set_defaults(func=cmd_autopilot)

    approve = sub.add_parser("approve", help="Review, approve, and optionally execute autopilot actions.")
    approve.add_argument("--list", action="store_true")
    approve.add_argument("--action", type=int)
    approve.add_argument("--execute", action="store_true")
    approve.add_argument("--limit", type=int, default=20)
    approve.set_defaults(func=cmd_approve)

    receipt = sub.add_parser("execution-receipt", help="Inspect action execution receipts.")
    receipt.add_argument("receipt_action", nargs="?", choices=["list", "show"], default="list")
    receipt.add_argument("--id", type=int)
    receipt.add_argument("--limit", type=int, default=20)
    receipt.set_defaults(func=cmd_execution_receipt)

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

    morning = sub.add_parser("morning", help="Show start-of-day priorities, risks, approvals, and evidence gaps.")
    morning.add_argument("--env-file", default="")
    morning.add_argument("--meeting-hours", type=float, default=0.0)
    morning.add_argument("--limit", type=int, default=5)
    morning.add_argument("--risk-threshold", type=int, default=60)
    morning.add_argument("--run-day", action="store_true", help="Run the older full run-day workflow instead of the brief.")
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
    if not _trace_enabled_for(args):
        args.func(args)
        return
    command = str(getattr(args, "command", "") or "unknown")
    command_path = _command_path(args)
    spec = command_registry.find_command(command)
    conn = get_connection()
    correlation_id = observability.start_trace(
        conn,
        command=command,
        command_path=command_path,
        parent_correlation_id=observability.current_correlation_id(),
        argv_hash=_argv_hash(sys.argv[1:]),
    )
    if spec:
        observability.link_trace(
            conn,
            correlation_id,
            intent=spec.intent,
            command_tier=spec.tier,
            safety_level=spec.safety,
        )
        conn.commit()
    previous_trace = os.environ.get(observability.TRACE_ENV)
    os.environ[observability.TRACE_ENV] = correlation_id
    started = time.monotonic()
    status = "completed"
    try:
        args.func(args)
    except SystemExit as exc:
        code = exc.code
        status = "completed" if code in (None, 0) else "failed"
        raise
    except Exception:
        status = "failed"
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        observability.finish_trace(
            conn,
            correlation_id,
            status=status,
            duration_ms=duration_ms,
            summary=f"{command_path} {status}",
            metadata={"command_path": command_path},
        )
        if previous_trace is None:
            os.environ.pop(observability.TRACE_ENV, None)
        else:
            os.environ[observability.TRACE_ENV] = previous_trace
        conn.close()


if __name__ == "__main__":
    main()
