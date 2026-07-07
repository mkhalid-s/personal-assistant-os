from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from . import cli_review
from .connectors import AhaConnector, ConfluenceConnector, GitHubConnector, JiraConnector
from .db import get_connection
from .extraction import extract_suggestions
from .inbox import (
    ensure_work_item_node,
    infer_from_external,
    infer_kind,
    infer_priority,
    infer_risk,
    index_chunk,
    insert_inbox_item_dedup,
)
from .locks import acquire_lock, release_lock


@dataclass(frozen=True)
class OperationsDependencies:
    load_env_file: Callable[[str], int]
    orchestrate_command: Callable[[argparse.Namespace], None] | None = None


def cmd_run_day(args: argparse.Namespace, deps: OperationsDependencies) -> dict[str, str] | None:
    if args.env_file:
        loaded = deps.load_env_file(args.env_file)
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
        print(
            f"Pipeline summary: external_ingested={external_created}, media_suggested={media_created}, triaged={triaged}"
        )

        cli_review.cmd_brief(
            argparse.Namespace(meeting_hours=args.meeting_hours, top=10, risk_threshold=args.risk_threshold)
        )
        print()
        cli_review.cmd_stop_doing(
            argparse.Namespace(
                capacity=args.capacity,
                deep_budget=args.deep_budget,
                keep_risk=args.keep_risk,
                limit=args.stop_limit,
            )
        )
        print()
        cli_review.cmd_report(
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


def cmd_go_live(args: argparse.Namespace, deps: OperationsDependencies) -> None:
    if args.env_file:
        loaded = deps.load_env_file(args.env_file)
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


def cmd_orchestrate(args: argparse.Namespace, deps: OperationsDependencies) -> None:
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
                lambda fn_args: cmd_run_day(fn_args, deps),
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
            step(
                "weekly_review",
                cli_review.cmd_weekly_review,
                argparse.Namespace(days=7, risk_threshold=args.risk_threshold, risk_alert=5),
            )
            step("metrics", cli_review.cmd_metrics, argparse.Namespace(days=7, risk_threshold=args.risk_threshold))
            step(
                "report",
                cli_review.cmd_report,
                argparse.Namespace(
                    meeting_hours=args.meeting_hours,
                    risk_threshold=args.risk_threshold,
                    output_dir=args.output_dir,
                ),
            )
        elif args.workflow == "incident":
            step("at_risk", cli_review.cmd_at_risk, argparse.Namespace(threshold=args.risk_threshold, limit=20))
            step(
                "renegotiate",
                cli_review.cmd_renegotiate,
                argparse.Namespace(days_ahead=2, default_extension_days=3, limit=20),
            )
            step(
                "next_action",
                cli_review.cmd_next_action,
                argparse.Namespace(meeting_hours=args.meeting_hours, risk_threshold=args.risk_threshold),
            )
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
            f"completed={stats['completed_c'] or 0}, skipped={stats['skipped_c'] or 0}, failed={stats['failed_c'] or 0}"
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


def cmd_worker(args: argparse.Namespace, deps: OperationsDependencies) -> None:
    conn = get_connection()
    try:
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
                orchestrate = deps.orchestrate_command or (lambda orch_args: cmd_orchestrate(orch_args, deps))
                orchestrate(
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
    finally:
        conn.close()
