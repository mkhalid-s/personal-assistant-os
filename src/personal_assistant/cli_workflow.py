from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .connectors import AhaConnector, ConfluenceConnector, GitHubConnector, JiraConnector
from .db import append_event, connection
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
from .ingest.audio import transcribe_audio
from .ingest.image import extract_image_text
from .locks import acquire_lock, release_lock
from .privacy import (
    _file_sha256,
    apply_privacy_filters,
    get_policy_map,
)
from .pulse import detect_mode, run_cycle


def cmd_capture(args: argparse.Namespace) -> None:
    with connection() as conn:
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
    with connection() as conn:
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
    json_mode = bool(getattr(args, "json", False))

    with connection() as conn:
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

    if json_mode:
        payload = {
            "schema": "myos.today.v1",
            "mode": str(mode),
            "meeting_hours": int(meeting_hours) if meeting_hours is not None else None,
            "top_outcomes": [
                {
                    "id": int(item["id"]),
                    "title": str(item["title"] or ""),
                    "kind": str(item["kind"] or ""),
                    "priority": int(item["priority"]) if item["priority"] is not None else None,
                    "risk_score": int(item["risk_score"]) if item["risk_score"] is not None else 0,
                    "due_date": str(item["due_date"] or ""),
                }
                for item in top_items[:3]
            ],
            "risk_watch": [
                {
                    "id": int(item["id"]),
                    "title": str(item["title"] or ""),
                    "risk_score": int(item["risk_score"]) if item["risk_score"] is not None else 0,
                    "due_date": str(item["due_date"] or ""),
                }
                for item in risky
            ],
        }
        print(json.dumps(payload, ensure_ascii=True))
        return

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
    with connection() as conn:
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
    with connection() as conn:
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
    with connection() as conn:
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
    with connection() as conn:
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


def cmd_transcribe(args: argparse.Namespace) -> None:
    audio_path = args.audio_file
    transcript = transcribe_audio(audio_path, args.text)
    if not transcript:
        print("No transcript produced. Install 'faster-whisper' or provide --text.")
        return

    with connection() as conn:
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

    with connection() as conn:
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
    print('Tip: run `myos context "<topic>"` to retrieve relevant chunks.')


def _is_watchable_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file() and path.suffix.lower() in {".txt", ".md", ".markdown", ".log"}


def _scan_watch_dirs(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    min_confidence: float = 0.65,
) -> tuple[int, int]:
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
    for watch_row in watch_dirs:
        root = Path(watch_row["path"]).expanduser()
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
                (watch_row["id"], str(path), file_hash),
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


def cmd_watch_dir(args: argparse.Namespace) -> None:
    with connection() as conn:
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
    with connection() as conn:
        files, suggestions = _scan_watch_dirs(conn, limit=args.limit, min_confidence=args.min_confidence)
        conn.commit()
    print(f"Watch scan complete: files_ingested={files}, suggestions_created={suggestions}")


def cmd_policy(args: argparse.Namespace) -> None:
    """Manage MYOS' `assistant_policies` key/value store (safe-mode toggles, etc.)."""
    with connection() as conn:
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


def cmd_pulse(args: argparse.Namespace, *, load_env_file: Callable[[str], int] | None = None) -> None:
    """Run the daily pulse cycle once or as a bounded loop with a coop lock."""
    if load_env_file is not None and args.env_file:
        load_env_file(args.env_file)
    if args.once:
        outputs = run_cycle(meeting_hours=args.meeting_hours)
        print("Pulse cycle done:", ", ".join(outputs))
        return
    with connection() as lock_conn:
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
