"""Autopilot helpers: signal detection, agent-task creation, safe-action execution,
and digest build/store/notify. Extracted from cli.py (refactor #12). The cycle
orchestrator _run_autopilot_cycle stays in cli because it drives other cmd_* commands.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import watch
from .db import append_event
from .execution import _execute_agent_action, _status_from_result
from .planner import _agent_analogies, _ai_reason_artifacts
from .privacy import apply_privacy_filters


def _create_agent_task(
    conn,
    *,
    objective: str,
    context: str,
    priority: int,
    mode: str,
    max_actions: int,
    analogy_limit: int = 5,
) -> int:
    objective = apply_privacy_filters(conn, objective)
    context = apply_privacy_filters(conn, context)
    analogies = _agent_analogies(conn, f"{objective} {context}", limit=analogy_limit)
    plan, actions, provider = _ai_reason_artifacts(
        conn,
        objective=objective,
        context=context,
        analogies=analogies,
        purpose="autopilot",
    )
    actions = actions[:max_actions]
    constraints = {"mode": mode, "max_actions": max_actions, "source": "autopilot"}

    conn.execute(
        """
        INSERT INTO agent_tasks (objective, context, constraints_json, priority, status)
        VALUES (?, ?, ?, ?, 'open')
        """,
        (objective, context, json.dumps(constraints, ensure_ascii=True), priority),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        """
        INSERT INTO agent_runs (agent_task_id, agent_name, provider, status, plan_json, summary, finished_at)
        VALUES (?, 'autopilot_v1', ?, 'completed', ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            task_id,
            provider,
            json.dumps(plan, ensure_ascii=True),
            f"Autopilot created {len(plan)} plan steps and {len(actions)} proposed actions.",
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
        conn.execute(
            """
            INSERT INTO agent_actions (agent_task_id, action_type, title, payload_json, requires_approval)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                action["action_type"],
                action["title"],
                json.dumps(action["payload"], ensure_ascii=True),
                action["requires_approval"],
            ),
        )
    append_event(
        conn,
        "autopilot_delegate",
        "agent_task",
        task_id,
        json.dumps({"actions": len(actions), "analogies": len(analogies)}, ensure_ascii=True),
    )
    return task_id


def _autopilot_signal_exists(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM autopilot_signals WHERE signal_key = ?", (key,)).fetchone() is not None


def _detect_autopilot_signals(conn, risk_threshold: int, due_days: int, limit: int, watch_risks: bool = False) -> list[dict[str, object]]:
    signals: list[dict[str, object]] = []
    delegated_work_items: set[int] = set()
    risk_rows = conn.execute(
        """
        SELECT id, title, kind, risk_score, due_date, owner
        FROM work_items
        WHERE status='open' AND risk_score >= ?
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT ?
        """,
        (risk_threshold, limit),
    ).fetchall()
    for row in risk_rows:
        delegated_work_items.add(int(row["id"]))
        signals.append(
            {
                "key": f"work_item:{row['id']}:risk:{row['risk_score']}",
                "type": "risk",
                "source_type": "work_item",
                "source_id": row["id"],
                "title": row["title"],
                "detail": f"risk={row['risk_score']} due={row['due_date'] or 'none'} owner={row['owner'] or 'none'}",
                "priority": 1,
            }
        )

    due_rows = conn.execute(
        """
        SELECT id, title, kind, risk_score, due_date, owner
        FROM work_items
        WHERE status='open'
          AND kind IN ('commitment', 'risk')
          AND due_date IS NOT NULL
          AND due_date <= date('now', ?)
        ORDER BY due_date ASC, risk_score DESC
        LIMIT ?
        """,
        (f"+{due_days} days", limit),
    ).fetchall()
    for row in due_rows:
        if int(row["id"]) in delegated_work_items:
            continue
        delegated_work_items.add(int(row["id"]))
        signals.append(
            {
                "key": f"work_item:{row['id']}:due:{row['due_date']}",
                "type": "due_soon",
                "source_type": "work_item",
                "source_id": row["id"],
                "title": row["title"],
                "detail": f"due={row['due_date']} risk={row['risk_score']} owner={row['owner'] or 'none'}",
                "priority": 1 if row["risk_score"] >= risk_threshold else 2,
            }
        )

    inbox_new = conn.execute("SELECT COUNT(*) AS c FROM inbox_items WHERE status='new'").fetchone()["c"]
    if inbox_new:
        signals.append(
            {
                "key": "inbox:new:backlog",
                "type": "inbox_backlog",
                "source_type": "inbox",
                "source_id": None,
                "title": f"{inbox_new} untriaged inbox items",
                "detail": "Autopilot should triage or summarize incoming unprocessed work.",
                "priority": 2,
            }
        )
    goal_rows = conn.execute(
        """
        SELECT id, objective, context, priority, cadence_minutes
        FROM assistant_goals
        WHERE status='active'
          AND (
            last_evaluated_at IS NULL
            OR last_evaluated_at <= datetime('now', '-' || cadence_minutes || ' minutes')
          )
        ORDER BY priority ASC, COALESCE(last_evaluated_at, '1970-01-01') ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in goal_rows:
        # Bucket the dedup key by the goal's cadence window so a fast cycle can't
        # spawn a new task every minute — at most one per cadence window. (audit B6)
        cadence = max(int(row["cadence_minutes"] or 1440), 1)
        cadence_bucket = int(time.time() // 60 // cadence)
        signals.append(
            {
                "key": f"goal:{row['id']}:{cadence_bucket}",
                "type": "standing_goal",
                "source_type": "assistant_goal",
                "source_id": row["id"],
                "title": row["objective"],
                "detail": row["context"] or "Evaluate this standing objective and decide whether action is needed.",
                "priority": row["priority"],
            }
        )
    # P4: proactive project-risk findings (opt-in via `myos autopilot --watch-risks`).
    # Stable keys → deduped across cycles, so an at-risk item yields one task, not churn.
    if watch_risks:
        signals.extend(watch.risk_signals(conn, risk_threshold=risk_threshold, limit=limit))
    return signals[:limit]


def _record_signal_and_task(conn, signal: dict[str, object], mode: str, max_actions: int) -> int | None:
    key = str(signal["key"])
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO autopilot_signals (signal_key, signal_type, source_type, source_id, title, detail, status)
        VALUES (?, ?, ?, ?, ?, ?, 'detected')
        """,
        (
            key,
            signal["type"],
            signal["source_type"],
            signal.get("source_id"),
            signal["title"],
            signal.get("detail", ""),
        ),
    )
    if cur.rowcount == 0:
        return None
    objective = f"Handle {signal['type']}: {signal['title']}"
    context = str(signal.get("detail", ""))
    task_id = _create_agent_task(
        conn,
        objective=objective,
        context=context,
        priority=int(signal.get("priority", 2)),
        mode=mode,
        max_actions=max_actions,
    )
    conn.execute(
        "UPDATE autopilot_signals SET status='delegated', agent_task_id=? WHERE signal_key=?",
        (task_id, key),
    )
    if signal["source_type"] == "assistant_goal" and signal.get("source_id") is not None:
        conn.execute(
            "UPDATE assistant_goals SET last_evaluated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (signal["source_id"],),
        )
    return task_id


def _execute_safe_autopilot_actions(conn, limit: int, task_ids: list[int]) -> int:
    if not task_ids:
        return 0
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM agent_actions
        WHERE status='proposed' AND requires_approval=0
          AND agent_task_id IN ({placeholders})
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (*task_ids, limit),
    ).fetchall()
    executed = 0
    for row in rows:
        claim = conn.execute(
            "UPDATE agent_actions SET status='executing' WHERE id=? AND status='proposed'",
            (row["id"],),
        )
        if claim.rowcount == 0:
            continue
        row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (row["id"],)).fetchone()
        result = _execute_agent_action(conn, row)
        new_status = _status_from_result(result)
        conn.execute(
            "UPDATE agent_actions SET status=?, "
            "executed_at=CASE WHEN ?='executed' THEN CURRENT_TIMESTAMP ELSE executed_at END, result=? WHERE id = ?",
            (new_status, new_status, result, row["id"]),
        )
        conn.execute(
            """
            INSERT INTO agent_observations (agent_task_id, observation_type, content, confidence)
            VALUES (?, 'autopilot_action_result', ?, 0.85)
            """,
            (row["agent_task_id"], f"action #{row['id']}: {result}"),
        )
        append_event(
            conn,
            "autopilot_action_executed",
            "agent_action",
            row["id"],
            json.dumps({"task_id": row["agent_task_id"], "result": result}, ensure_ascii=True),
        )
        if new_status == "executed":
            executed += 1
    return executed


def _build_autopilot_digest(
    conn,
    *,
    run_id: int,
    synced: int,
    signals_detected: int,
    tasks_created: int,
    safe_actions: int,
    approvals_pending: int,
    created_task_ids: list[int],
) -> tuple[str, str, dict[str, object]]:
    new_tasks = []
    if created_task_ids:
        placeholders = ",".join("?" for _ in created_task_ids)
        new_tasks = conn.execute(
            f"""
            SELECT id, objective, priority, status
            FROM agent_tasks
            WHERE id IN ({placeholders})
            ORDER BY priority ASC, id ASC
            """,
            tuple(created_task_ids),
        ).fetchall()
    pending = conn.execute(
        """
        SELECT id, agent_task_id, action_type, title, payload_json
        FROM agent_actions
        WHERE status='proposed' AND requires_approval=1
        ORDER BY created_at ASC
        LIMIT 10
        """
    ).fetchall()
    top_risk = conn.execute(
        """
        SELECT id, title, risk_score, due_date
        FROM work_items
        WHERE status='open'
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 3
        """
    ).fetchall()

    title = f"Autopilot digest #{run_id}: {tasks_created} new tasks, {approvals_pending} approvals"
    lines = [
        f"# {title}",
        "",
        "## What I handled",
        f"- Synced systems: {'yes' if synced else 'no'}",
        f"- Signals detected: {signals_detected}",
        f"- New assistant tasks: {tasks_created}",
        f"- Safe local actions executed: {safe_actions}",
        "",
        "## New tasks",
    ]
    if new_tasks:
        for row in new_tasks:
            objective = apply_privacy_filters(conn, row["objective"])
            lines.append(f"- #{row['id']} priority={row['priority']} status={row['status']}: {objective}")
    else:
        lines.append("- None")

    lines.extend(["", "## Needs your approval"])
    if pending:
        for row in pending:
            payload = json.loads(row["payload_json"] or "{}")
            preview = payload.get("draft") or payload.get("text") or ""
            preview = apply_privacy_filters(conn, str(preview)).replace("\n", " ")
            if len(preview) > 180:
                preview = preview[:177] + "..."
            suffix = f" - {preview}" if preview else ""
            title = apply_privacy_filters(conn, row["title"])
            lines.append(f"- action #{row['id']} task=#{row['agent_task_id']} [{row['action_type']}] {title}{suffix}")
    else:
        lines.append("- None")

    lines.extend(["", "## Risk watch"])
    if top_risk:
        for row in top_risk:
            title = apply_privacy_filters(conn, row["title"])
            lines.append(f"- #{row['id']} risk={row['risk_score']} due={row['due_date'] or 'none'}: {title}")
    else:
        lines.append("- No open risks.")

    if pending:
        next_step = "Review approval queue: myos approve --list"
    elif top_risk:
        next_step = "Focus the highest risk item or let autopilot continue monitoring."
    else:
        next_step = "No immediate intervention needed."
    lines.extend(["", "## Recommended next step", f"- {next_step}"])

    payload = {
        "run_id": run_id,
        "synced": synced,
        "signals_detected": signals_detected,
        "tasks_created": tasks_created,
        "safe_actions": safe_actions,
        "approvals_pending": approvals_pending,
        "created_task_ids": created_task_ids,
        "recommended_next_step": next_step,
    }
    return title, "\n".join(lines) + "\n", payload


def _store_autopilot_digest(conn, title: str, body: str, payload: dict[str, object], output_dir: str = "") -> int:
    conn.execute(
        """
        INSERT INTO assistant_digests (autopilot_run_id, title, body, payload_json)
        VALUES (?, ?, ?, ?)
        """,
        (payload.get("run_id"), title, body, json.dumps(payload, ensure_ascii=True)),
    )
    digest_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    out_dir = Path(output_dir) if output_dir else Path(__file__).resolve().parents[2] / "data" / "autopilot"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.md").write_text(body)
    (out_dir / f"digest-{digest_id}.md").write_text(body)
    return digest_id


def _notify_digest(conn, digest_id: int, title: str, body: str, payload: dict[str, object]) -> None:
    command = os.getenv("MYOS_NOTIFY_COMMAND", "").strip()
    if not command:
        return
    notify_payload = {
        "digest_id": digest_id,
        "title": title,
        "body": body,
        "payload": payload,
    }
    started = time.monotonic()
    try:
        proc = subprocess.run(
            shlex.split(command),
            input=json.dumps(notify_payload, ensure_ascii=True),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        status = "ok" if proc.returncode == 0 else "error"
        detail = (proc.stdout if proc.returncode == 0 else proc.stderr or proc.stdout)[:1000]
    except Exception as exc:
        status = "error"
        detail = str(exc)[:1000]
    append_event(
        conn,
        "assistant_digest_notify",
        "assistant_digest",
        digest_id,
        json.dumps(
            {
                "status": status,
                "detail": detail,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
            ensure_ascii=True,
        ),
    )
