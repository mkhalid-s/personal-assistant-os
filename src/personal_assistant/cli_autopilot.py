from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import autonomy_loop, context as ctx, factory, router
from .autopilot import (
    _build_autopilot_digest,
    _detect_autopilot_signals,
    _execute_safe_autopilot_actions,
    _notify_digest,
    _record_signal_and_task,
    _store_autopilot_digest,
)
from .db import append_event, get_connection
from .locks import acquire_lock, release_lock


@dataclass(frozen=True)
class AutopilotCommandDependencies:
    load_env_file: Callable[[str], int]
    cmd_sync: Callable[[argparse.Namespace], None]
    cmd_ingest_external: Callable[[argparse.Namespace], None]
    scan_watch_dirs: Callable[..., tuple[int, int]]
    cmd_inbox_process: Callable[[argparse.Namespace], None]
    cmd_triage: Callable[[argparse.Namespace], None]
    print_goal_cycle_result: Callable[[dict[str, object]], None]


def run_autopilot_cycle(args: argparse.Namespace, deps: AutopilotCommandDependencies) -> dict[str, int]:
    if args.env_file:
        loaded = deps.load_env_file(args.env_file)
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
            deps.cmd_sync(argparse.Namespace(connector=args.connector, env_file=""))
            synced = 1
        if not args.no_process:
            deps.cmd_ingest_external(argparse.Namespace(limit=args.external_limit, min_risk=55))
            watched_files, watch_suggestions = deps.scan_watch_dirs(
                conn,
                limit=args.watch_limit,
                min_confidence=args.min_confidence,
            )
            conn.commit()
            deps.cmd_inbox_process(argparse.Namespace(limit=args.media_limit, min_confidence=args.min_confidence))
            deps.cmd_triage(argparse.Namespace())

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
        append_event(
            conn, "autopilot_cycle", "autopilot_run", run_id, json.dumps({"summary": summary}, ensure_ascii=True)
        )
        conn.commit()
        try:
            reflection = ctx.reflect(conn)
            hygiene_stats = ctx.hygiene(conn)
            append_event(
                conn,
                "context_reflect",
                "autopilot_run",
                run_id,
                json.dumps({**reflection, **hygiene_stats}, ensure_ascii=True),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - reflection cannot fail an autopilot cycle
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
            print("Run: myos approve --list [label=review_approvals]")
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


def run_autopilot_goal_cycle(args: argparse.Namespace, deps: AutopilotCommandDependencies) -> dict[str, object]:
    if args.env_file:
        loaded = deps.load_env_file(args.env_file)
        print(f"Loaded {loaded} vars from {args.env_file}")
    conn = get_connection()
    conn.execute(
        "INSERT INTO autopilot_runs (status, mode) VALUES ('running', 'loop_goal')",
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.commit()
    try:
        result = autonomy_loop.run_goal_cycle(
            conn,
            goal_id=args.loop_goal_id,
            backend="",
            max_actions=args.max_actions,
            limit=args.loop_goal_limit,
        )
        ledger_rows = autonomy_loop.list_ledger(conn, limit=1)
        ledger_id = int(ledger_rows[0]["id"]) if ledger_rows else 0
        action = str(result.get("action") or "")
        summary = (
            f"loop_goal action={action} goal={result.get('goal_id')} "
            f"task={result.get('task_id')} ledger={ledger_id} "
            f"pending_approvals={result.get('pending_approvals', 0)}"
        )
        conn.execute(
            """
            UPDATE autopilot_runs
            SET status='completed', finished_at=CURRENT_TIMESTAMP,
                tasks_created=?, safe_actions_executed=?, approvals_pending=?, summary=?
            WHERE id=?
            """,
            (
                1 if action == "started" else 0,
                int(result.get("executed_now") or 0),
                int(result.get("pending_approvals") or 0),
                summary,
                run_id,
            ),
        )
        append_event(
            conn,
            "autopilot_loop_goal",
            "autopilot_run",
            run_id,
            json.dumps({"summary": summary, "ledger_id": ledger_id, "action": action}, ensure_ascii=True),
        )
        conn.commit()
        print(f"Autopilot goal wrapper complete (run_id={run_id}, ledger_id={ledger_id}): {summary}")
        deps.print_goal_cycle_result(result)
        if ledger_id:
            print("Ledger: myos loop ledger --limit 1")
        return {"run_id": run_id, "ledger_id": ledger_id, **result}
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE autopilot_runs SET status='failed', finished_at=CURRENT_TIMESTAMP, summary=? WHERE id=?",
            (str(exc), run_id),
        )
        conn.commit()
        raise


def cmd_autopilot(args: argparse.Namespace, deps: AutopilotCommandDependencies) -> None:
    lock_conn = get_connection()
    owner = f"autopilot-{os.getpid()}"
    if getattr(args, "loop_goal", False):
        if not args.once:
            print("autopilot --loop-goal is one-shot only. Re-run with --once.")
            raise SystemExit(1)
        if acquire_lock(lock_conn, "autopilot", owner):
            try:
                run_autopilot_goal_cycle(args, deps)
            finally:
                release_lock(lock_conn, "autopilot", owner)
                lock_conn.commit()
        else:
            print("autopilot: another instance is mid-cycle; skipping this tick.")
        return
    cycles = 0
    while True:
        if acquire_lock(lock_conn, "autopilot", owner):
            try:
                run_autopilot_cycle(args, deps)
            finally:
                release_lock(lock_conn, "autopilot", owner)
                lock_conn.commit()
        else:
            print("autopilot: another instance is mid-cycle; skipping this tick.")
        cycles += 1
        if args.once or (args.max_cycles and cycles >= args.max_cycles):
            return
        time.sleep(args.interval_sec)
