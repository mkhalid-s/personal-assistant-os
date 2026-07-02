"""Durable autonomous task loop built on existing MYOS agent tables.

This module intentionally avoids a new scheduler or execution path. Each call runs
one bounded reasoning cycle, records it in ``agent_runs``, executes only safe local
actions, and leaves risky work in the existing approval queue.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import agentcore, observability, providers
from .db import append_event
from .execution import _execute_agent_action, _status_from_result
from .planner import _agent_action_specs, _agent_analogies, _agent_plan, _normalize_ai_actions, _normalize_ai_plan
from .privacy import apply_privacy_filters

LOOP_SOURCE = "autonomy_loop"
MODES = ("safe", "balanced")
DEFAULT_MAX_ACTIONS = 5


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_json(value: str | None, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except Exception:
        return default
    return parsed if parsed is not None else default


def _constraints(
    *,
    mode: str,
    backend: str,
    max_actions: int,
    cycles: int = 0,
    goal_id: int | None = None,
) -> dict[str, object]:
    meta: dict[str, object] = {
        "source": LOOP_SOURCE,
        "mode": mode if mode in MODES else "safe",
        "backend": backend,
        "max_actions": max(1, int(max_actions or DEFAULT_MAX_ACTIONS)),
        "cycles": max(0, int(cycles or 0)),
    }
    if goal_id is not None:
        meta["goal_id"] = int(goal_id)
    return meta


def _record_observation(conn: sqlite3.Connection, task_id: int, kind: str, content: str, confidence: float = 0.8) -> None:
    conn.execute(
        """
        INSERT INTO agent_observations (agent_task_id, observation_type, content, confidence)
        VALUES (?, ?, ?, ?)
        """,
        (int(task_id), kind, apply_privacy_filters(conn, content)[:2000], float(confidence)),
    )


def _task(conn: sqlite3.Connection, task_id: int):
    row = conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (int(task_id),)).fetchone()
    if not row:
        raise ValueError(f"autonomy loop task not found: {task_id}")
    constraints = _load_json(row["constraints_json"], {})
    if constraints.get("source") != LOOP_SOURCE:
        raise ValueError(f"agent task #{task_id} is not an autonomy loop task")
    return row, constraints


def _reason(conn: sqlite3.Connection, *, objective: str, context: str, backend_name: str, purpose: str) -> tuple[list[dict], list[dict], str, str]:
    analogies = _agent_analogies(conn, f"{objective} {context}", limit=5)
    provider = "local_loop"
    reply = ""
    if backend_name:
        try:
            backend = providers.get_backend(backend_name)
            ok, detail = backend.available()
            if ok:
                result = backend.reason(
                    conn,
                    {
                        "purpose": purpose,
                        "objective": objective,
                        "context": context,
                        "analogies": [
                            {"score": score, "source": source, "content": apply_privacy_filters(conn, content)}
                            for score, source, content in analogies
                        ],
                    },
                )
                plan = _normalize_ai_plan(result.get("plan"))
                actions = _normalize_ai_actions(result.get("actions"))
                reply = str(result.get("reply") or "")[:2000]
                if plan or actions:
                    return plan, actions, backend.name, reply
                provider = backend.name
                reply = reply or f"Backend {backend.name} returned no structured actions; using local fallback."
            else:
                reply = f"Backend {backend.name} unavailable: {detail}; using local fallback."
        except Exception as exc:  # noqa: BLE001 - durable loop should degrade to local planning
            reply = f"Backend {backend_name} failed: {exc}; using local fallback."[:2000]
    plan = _agent_plan(objective, context, len(analogies))
    actions = _agent_action_specs(objective, context, plan)
    return plan, actions, provider, reply


def _enqueue_actions(conn: sqlite3.Connection, task_id: int, actions: list[dict], max_actions: int) -> list[int]:
    ids: list[int] = []
    for action in actions[: max(1, int(max_actions or DEFAULT_MAX_ACTIONS))]:
        ids.append(
            agentcore.enqueue_proposal(
                conn,
                task_id=task_id,
                action_type=str(action.get("action_type") or "draft_external_update"),
                title=str(action.get("title") or "Autonomy loop proposal"),
                payload=action.get("payload") if isinstance(action.get("payload"), dict) else {},
                requires_approval=_safe_int(action.get("requires_approval"), 1),
            )
        )
    return ids


def _execute_safe_actions(conn: sqlite3.Connection, task_id: int, action_ids: list[int]) -> int:
    if not action_ids:
        return 0
    placeholders = ",".join("?" for _ in action_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM agent_actions
        WHERE agent_task_id = ?
          AND id IN ({placeholders})
          AND status = 'proposed'
          AND requires_approval = 0
        ORDER BY id ASC
        """,
        (int(task_id), *[int(action_id) for action_id in action_ids]),
    ).fetchall()
    executed = 0
    for row in rows:
        result = _execute_agent_action(conn, row)
        status = _status_from_result(result)
        conn.execute(
            """
            UPDATE agent_actions
            SET status = ?, executed_at = CURRENT_TIMESTAMP, result = ?
            WHERE id = ?
            """,
            (status, apply_privacy_filters(conn, result)[:1000], int(row["id"])),
        )
        _record_observation(conn, task_id, f"safe_action_{status}", f"action #{row['id']}: {result}")
        if status == "executed":
            executed += 1
    return executed


def _counts(conn: sqlite3.Connection, task_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total_actions,
          SUM(CASE WHEN status = 'proposed' AND requires_approval = 1 THEN 1 ELSE 0 END) AS pending_approvals,
          SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) AS executed,
          SUM(CASE WHEN status IN ('blocked', 'failed') THEN 1 ELSE 0 END) AS blocked_or_failed
        FROM agent_actions
        WHERE agent_task_id = ?
        """,
        (int(task_id),),
    ).fetchone()
    return {
        "total_actions": int(row["total_actions"] or 0),
        "pending_approvals": int(row["pending_approvals"] or 0),
        "executed": int(row["executed"] or 0),
        "blocked_or_failed": int(row["blocked_or_failed"] or 0),
    }


def _finish_cycle(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    run_id: int,
    plan: list[dict],
    provider: str,
    action_ids: list[int],
    executed_now: int,
    reply: str = "",
) -> dict[str, object]:
    counts = _counts(conn, task_id)
    if counts["pending_approvals"]:
        status = "waiting_approval"
    elif counts["blocked_or_failed"]:
        status = "blocked"
    elif counts["total_actions"]:
        status = "completed"
    else:
        status = "waiting"
    summary = (
        f"Autonomy loop cycle proposed {len(action_ids)} action(s), "
        f"executed {executed_now} safe action(s), "
        f"pending approvals={counts['pending_approvals']}."
    )
    if reply:
        _record_observation(conn, task_id, "provider_reply", reply, 0.7)
    conn.execute(
        """
        UPDATE agent_runs
        SET status = 'completed', plan_json = ?, summary = ?, finished_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (json.dumps(plan, ensure_ascii=True), summary, int(run_id)),
    )
    conn.execute(
        "UPDATE agent_tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, int(task_id)),
    )
    _record_observation(conn, task_id, "cycle_summary", summary)
    append_event(
        conn,
        "autonomy_loop_cycle",
        "agent_task",
        int(task_id),
        json.dumps({"run_id": int(run_id), "provider": provider, **counts, "executed_now": executed_now}, ensure_ascii=True),
    )
    observability.link_current_trace(
        conn,
        agent_task_id=int(task_id),
    )
    result = {
        "task_id": int(task_id),
        "run_id": int(run_id),
        "status": status,
        "provider": provider,
        "plan": plan,
        "action_ids": action_ids,
        "executed_now": executed_now,
        **counts,
        "summary": summary,
    }
    conn.commit()
    return result


def eligible_goals(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id, objective, context, priority, cadence_minutes, last_evaluated_at
        FROM assistant_goals
        WHERE status = 'active'
          AND (
            last_evaluated_at IS NULL
            OR last_evaluated_at <= datetime('now', '-' || cadence_minutes || ' minutes')
          )
        ORDER BY priority ASC, COALESCE(last_evaluated_at, '1970-01-01') ASC, id ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out: list[dict[str, object]] = []
    for row in rows:
        loop = find_goal_loop(conn, int(row["id"]))
        out.append(
            {
                "goal_id": int(row["id"]),
                "objective": str(row["objective"]),
                "context": str(row["context"] or ""),
                "priority": int(row["priority"] or 2),
                "cadence_minutes": int(row["cadence_minutes"] or 1440),
                "last_evaluated_at": row["last_evaluated_at"],
                "loop": loop,
            }
        )
    return out


def _get_goal(conn: sqlite3.Connection, goal_id: int) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT id, objective, context, priority, cadence_minutes, last_evaluated_at
        FROM assistant_goals
        WHERE id = ? AND status = 'active'
        """,
        (int(goal_id),),
    ).fetchone()
    if not row:
        return None
    return {
        "goal_id": int(row["id"]),
        "objective": str(row["objective"]),
        "context": str(row["context"] or ""),
        "priority": int(row["priority"] or 2),
        "cadence_minutes": int(row["cadence_minutes"] or 1440),
        "last_evaluated_at": row["last_evaluated_at"],
        "loop": find_goal_loop(conn, int(row["id"])),
    }


def find_goal_loop(conn: sqlite3.Connection, goal_id: int) -> dict[str, object] | None:
    rows = conn.execute(
        """
        SELECT *
        FROM agent_tasks
        ORDER BY updated_at DESC, id DESC
        LIMIT 500
        """
    ).fetchall()
    for row in rows:
        meta = _load_json(row["constraints_json"], {})
        if meta.get("source") != LOOP_SOURCE or _safe_int(meta.get("goal_id"), -1) != int(goal_id):
            continue
        counts = _counts(conn, int(row["id"]))
        return {
            "task_id": int(row["id"]),
            "status": str(row["status"]),
            "cycles": _safe_int(meta.get("cycles"), 0),
            "backend": str(meta.get("backend") or ""),
            "mode": str(meta.get("mode") or "safe"),
            **counts,
        }
    return None


def _mark_goal_evaluated(conn: sqlite3.Connection, goal_id: int) -> None:
    conn.execute(
        "UPDATE assistant_goals SET last_evaluated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (int(goal_id),),
    )


def _record_ledger(
    conn: sqlite3.Connection,
    *,
    decision_type: str,
    status: str,
    reason: str = "",
    assistant_goal_id: int | None = None,
    agent_task_id: int | None = None,
    agent_run_id: int | None = None,
    provider: str = "",
    actions_proposed: int = 0,
    safe_actions_executed: int = 0,
    pending_approvals: int = 0,
    blocked_or_failed: int = 0,
    metadata: dict[str, object] | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO autonomy_run_ledger (
            decision_type, status, reason, assistant_goal_id, agent_task_id, agent_run_id,
            correlation_id, provider, actions_proposed, safe_actions_executed,
            pending_approvals, blocked_or_failed, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_type[:80],
            status[:80],
            apply_privacy_filters(conn, reason)[:1000],
            int(assistant_goal_id) if assistant_goal_id is not None else None,
            int(agent_task_id) if agent_task_id is not None else None,
            int(agent_run_id) if agent_run_id is not None else None,
            observability.current_correlation_id()[:120],
            provider[:80],
            max(0, int(actions_proposed or 0)),
            max(0, int(safe_actions_executed or 0)),
            max(0, int(pending_approvals or 0)),
            max(0, int(blocked_or_failed or 0)),
            json.dumps(metadata or {}, ensure_ascii=True)[:2000],
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _record_result_ledger(
    conn: sqlite3.Connection,
    *,
    decision_type: str,
    result: dict[str, object],
    assistant_goal_id: int | None = None,
    metadata: dict[str, object] | None = None,
) -> int:
    return _record_ledger(
        conn,
        decision_type=decision_type,
        status=str(result.get("status") or ""),
        reason=str(result.get("summary") or ""),
        assistant_goal_id=assistant_goal_id,
        agent_task_id=_safe_int(result.get("task_id"), 0) or None,
        agent_run_id=_safe_int(result.get("run_id"), 0) or None,
        provider=str(result.get("provider") or ""),
        actions_proposed=len(result.get("action_ids") or []),
        safe_actions_executed=_safe_int(result.get("executed_now"), 0),
        pending_approvals=_safe_int(result.get("pending_approvals"), 0),
        blocked_or_failed=_safe_int(result.get("blocked_or_failed"), 0),
        metadata=metadata,
    )


def list_ledger(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    goal_id: int | None = None,
    task_id: int | None = None,
    status: str = "",
) -> list[dict[str, object]]:
    clauses: list[str] = []
    values: list[object] = []
    if goal_id is not None:
        clauses.append("assistant_goal_id = ?")
        values.append(int(goal_id))
    if task_id is not None:
        clauses.append("agent_task_id = ?")
        values.append(int(task_id))
    if status:
        clauses.append("status = ?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, decision_type, status, reason, assistant_goal_id, agent_task_id,
               agent_run_id, correlation_id, provider, actions_proposed,
               safe_actions_executed, pending_approvals, blocked_or_failed,
               metadata_json, created_at
        FROM autonomy_run_ledger
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*values, max(1, int(limit))),
    ).fetchall()
    return [dict(row) for row in rows]


def start_loop(
    conn: sqlite3.Connection,
    objective: str,
    *,
    context: str = "",
    backend: str = "",
    max_actions: int = DEFAULT_MAX_ACTIONS,
    mode: str = "safe",
    goal_id: int | None = None,
) -> dict[str, object]:
    objective = apply_privacy_filters(conn, objective).strip()
    context = apply_privacy_filters(conn, context).strip()
    if not objective:
        raise ValueError("autonomy loop objective is required")
    meta = _constraints(mode=mode, backend=backend, max_actions=max_actions, cycles=1, goal_id=goal_id)
    conn.execute(
        """
        INSERT INTO agent_tasks (objective, context, constraints_json, priority, status)
        VALUES (?, ?, ?, 2, 'running')
        """,
        (objective[:2000], context[:2000], json.dumps(meta, ensure_ascii=True)),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    observability.link_current_trace(conn, agent_task_id=task_id)
    result = _run_cycle(conn, task_id=task_id, max_actions=max_actions)
    _record_result_ledger(
        conn,
        decision_type="loop_started",
        result=result,
        assistant_goal_id=goal_id,
        metadata={"mode": mode, "max_actions": max_actions},
    )
    conn.commit()
    return result


def resume_loop(conn: sqlite3.Connection, task_id: int, *, max_actions: int | None = None) -> dict[str, object]:
    row, meta = _task(conn, task_id)
    counts = _counts(conn, int(task_id))
    if counts["pending_approvals"]:
        latest_run = conn.execute(
            """
            SELECT id, provider
            FROM agent_runs
            WHERE agent_task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(task_id),),
        ).fetchone()
        summary = f"Autonomy loop paused with {counts['pending_approvals']} pending approval(s)."
        _record_observation(conn, int(task_id), "resume_paused", summary)
        append_event(
            conn,
            "autonomy_loop_paused",
            "agent_task",
            int(task_id),
            json.dumps({"pending_approvals": counts["pending_approvals"]}, ensure_ascii=True),
        )
        observability.link_current_trace(conn, agent_task_id=int(task_id))
        result = {
            "task_id": int(task_id),
            "run_id": int(latest_run["id"]) if latest_run else 0,
            "status": str(row["status"]),
            "provider": str(latest_run["provider"]) if latest_run else "",
            "plan": [],
            "action_ids": [],
            "executed_now": 0,
            **counts,
            "summary": summary,
        }
        _record_result_ledger(
            conn,
            decision_type="loop_paused",
            result=result,
            assistant_goal_id=_safe_int(meta.get("goal_id"), 0) or None,
            metadata={"reason": "pending_approvals"},
        )
        conn.commit()
        return result
    cycles = _safe_int(meta.get("cycles"), 0) + 1
    if max_actions is not None:
        meta["max_actions"] = max(1, int(max_actions))
    meta["cycles"] = cycles
    conn.execute(
        "UPDATE agent_tasks SET constraints_json = ?, status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(meta, ensure_ascii=True), int(task_id)),
    )
    observability.link_current_trace(conn, agent_task_id=int(task_id))
    result = _run_cycle(conn, task_id=int(row["id"]), max_actions=_safe_int(meta.get("max_actions"), DEFAULT_MAX_ACTIONS))
    _record_result_ledger(
        conn,
        decision_type="loop_resumed",
        result=result,
        assistant_goal_id=_safe_int(meta.get("goal_id"), 0) or None,
        metadata={"cycles": cycles, "max_actions": _safe_int(meta.get("max_actions"), DEFAULT_MAX_ACTIONS)},
    )
    conn.commit()
    return result


def run_goal_cycle(
    conn: sqlite3.Connection,
    *,
    goal_id: int | None = None,
    backend: str = "",
    max_actions: int = DEFAULT_MAX_ACTIONS,
    limit: int = 5,
) -> dict[str, object]:
    if goal_id is not None:
        goal = _get_goal(conn, int(goal_id))
        if goal is None:
            summary = f"No active assistant goal found for id={goal_id}."
            append_event(conn, "autonomy_goal_noop", "assistant_goal", int(goal_id), summary)
            _record_ledger(
                conn,
                decision_type="goal_noop",
                status="noop",
                reason=summary,
                assistant_goal_id=int(goal_id),
                metadata={"reason": "goal_not_active"},
            )
            conn.commit()
            return {"action": "noop", "goal_id": int(goal_id), "summary": summary}
    else:
        goals = eligible_goals(conn, limit=limit)
        if not goals:
            summary = "No eligible assistant goals are due for an autonomy loop cycle."
            append_event(conn, "autonomy_goal_noop", "assistant_goal", None, summary)
            _record_ledger(
                conn,
                decision_type="goal_noop",
                status="noop",
                reason=summary,
                metadata={"reason": "no_eligible_goals", "limit": max(1, int(limit))},
            )
            conn.commit()
            return {"action": "noop", "goal_id": None, "summary": summary}
        goal = goals[0]

    selected_goal_id = int(goal["goal_id"])
    loop = goal.get("loop")
    if isinstance(loop, dict) and int(loop.get("pending_approvals") or 0) > 0:
        task_id = int(loop["task_id"])
        summary = (
            f"Goal #{selected_goal_id} skipped because loop task #{task_id} "
            f"has {loop['pending_approvals']} pending approval(s)."
        )
        _record_observation(conn, task_id, "scheduler_skip", summary)
        append_event(
            conn,
            "autonomy_goal_skipped",
            "assistant_goal",
            selected_goal_id,
            json.dumps({"agent_task_id": task_id, "reason": summary}, ensure_ascii=True),
        )
        observability.link_current_trace(conn, agent_task_id=task_id)
        _mark_goal_evaluated(conn, selected_goal_id)
        _record_ledger(
            conn,
            decision_type="goal_skipped",
            status="skipped",
            reason=summary,
            assistant_goal_id=selected_goal_id,
            agent_task_id=task_id,
            pending_approvals=int(loop["pending_approvals"]),
            blocked_or_failed=_safe_int(loop.get("blocked_or_failed"), 0),
            metadata={"reason": "pending_approvals"},
        )
        conn.commit()
        return {
            "action": "skipped",
            "goal_id": selected_goal_id,
            "task_id": task_id,
            "pending_approvals": int(loop["pending_approvals"]),
            "summary": summary,
        }

    if isinstance(loop, dict):
        result = resume_loop(conn, int(loop["task_id"]), max_actions=max_actions)
        event_type = "autonomy_goal_resumed"
        action = "resumed"
    else:
        result = start_loop(
            conn,
            str(goal["objective"]),
            context=str(goal.get("context") or ""),
            backend=backend,
            max_actions=max_actions,
            mode="safe",
            goal_id=selected_goal_id,
        )
        event_type = "autonomy_goal_started"
        action = "started"

    append_event(
        conn,
        event_type,
        "assistant_goal",
        selected_goal_id,
        json.dumps(
            {
                "agent_task_id": int(result["task_id"]),
                "run_id": int(result["run_id"]),
                "status": result["status"],
                "pending_approvals": int(result["pending_approvals"]),
                "executed_now": int(result["executed_now"]),
            },
            ensure_ascii=True,
        ),
    )
    _mark_goal_evaluated(conn, selected_goal_id)
    _record_result_ledger(
        conn,
        decision_type=f"goal_{action}",
        result=result,
        assistant_goal_id=selected_goal_id,
        metadata={"event_type": event_type},
    )
    conn.commit()
    return {"action": action, "goal_id": selected_goal_id, **result}


def _run_cycle(conn: sqlite3.Connection, *, task_id: int, max_actions: int) -> dict[str, object]:
    row, meta = _task(conn, task_id)
    backend_name = str(meta.get("backend") or "")
    plan, actions, provider, reply = _reason(
        conn,
        objective=str(row["objective"]),
        context=str(row["context"] or ""),
        backend_name=backend_name,
        purpose="autonomy_loop",
    )
    conn.execute(
        """
        INSERT INTO agent_runs (agent_task_id, agent_name, provider, status, plan_json, summary)
        VALUES (?, 'autonomy_loop_v1', ?, 'running', ?, ?)
        """,
        (int(task_id), provider, json.dumps(plan, ensure_ascii=True), "Autonomy loop cycle running."),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    action_ids = _enqueue_actions(conn, int(task_id), actions, max_actions)
    executed_now = _execute_safe_actions(conn, int(task_id), action_ids)
    return _finish_cycle(
        conn,
        task_id=int(task_id),
        run_id=run_id,
        plan=plan,
        provider=provider,
        action_ids=action_ids,
        executed_now=executed_now,
        reply=reply,
    )


def loop_status(conn: sqlite3.Connection, task_id: int | None = None, *, limit: int = 10) -> list[dict[str, object]]:
    values: list[object] = []
    where = ""
    if task_id is not None:
        where = "WHERE id = ?"
        values.append(int(task_id))
    rows = conn.execute(
        f"""
        SELECT *
        FROM agent_tasks
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT 200
        """,
        values,
    ).fetchall()
    out: list[dict[str, object]] = []
    for row in rows:
        meta = _load_json(row["constraints_json"], {})
        if meta.get("source") != LOOP_SOURCE:
            continue
        counts = _counts(conn, int(row["id"]))
        latest_run = conn.execute(
            """
            SELECT id, provider, status, summary, finished_at
            FROM agent_runs
            WHERE agent_task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(row["id"]),),
        ).fetchone()
        out.append(
            {
                "task_id": int(row["id"]),
                "objective": str(row["objective"]),
                "status": str(row["status"]),
                "mode": str(meta.get("mode") or "safe"),
                "backend": str(meta.get("backend") or ""),
                "cycles": _safe_int(meta.get("cycles"), 0),
                "latest_run_id": int(latest_run["id"]) if latest_run else None,
                "provider": str(latest_run["provider"]) if latest_run else "",
                "summary": str(latest_run["summary"] or "") if latest_run else "",
                **counts,
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out
