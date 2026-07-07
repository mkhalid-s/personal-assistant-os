from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any

from . import agentcore, autonomy, graphrag, intents, observability, plans, providers, zero_executor
from .db import append_event
from .execution import approve_and_execute
from .inbox import insert_inbox_item_dedup
from .privacy import apply_privacy_filters

MODES = ("review_first", "semi_autonomous", "full_autonomous")
WORKFLOW_PACKS = ("intent_execution", "daily_ops", "software_delivery", "connector_ops")
STAGES = ("context", "planner", "researcher", "executor", "reviewer", "critic", "approval", "execution", "learning")
_MODE_RANK = {mode: idx for idx, mode in enumerate(MODES)}
_MAX_FULL_AUTO_ACTIONS = 5
_MAX_ZERO_PATCH_BYTES = 200000


def mode_allowed(conn: sqlite3.Connection, *, intent_id: int, requested_mode: str) -> tuple[bool, str]:
    requested_mode = requested_mode if requested_mode in MODES else "review_first"
    if requested_mode == "review_first":
        return True, "review_first is always allowed"
    rows = conn.execute(
        """
        SELECT allowed_mode, scope_type, scope_id
        FROM factory_policies
        WHERE status = 'active'
          AND connector = ''
          AND action_type = ''
          AND (
            (scope_type = 'intent' AND scope_id = ?)
            OR (scope_type = 'global' AND scope_id = '')
          )
        ORDER BY CASE scope_type WHEN 'intent' THEN 0 ELSE 1 END, id DESC
        """,
        (str(intent_id),),
    ).fetchall()
    for row in rows:
        allowed_mode = row["allowed_mode"] if row["allowed_mode"] in MODES else "review_first"
        if _MODE_RANK[allowed_mode] >= _MODE_RANK[requested_mode]:
            return True, f"{row['scope_type']} policy allows {allowed_mode}"
    return False, f"{requested_mode} requires an explicit factory policy"


def connector_action_allowed(
    conn: sqlite3.Connection,
    *,
    requested_mode: str,
    connector: str = "",
    action_type: str = "",
) -> tuple[bool, str]:
    requested_mode = requested_mode if requested_mode in MODES else "review_first"
    if not connector and not action_type:
        return True, "no connector/action policy needed"
    row = conn.execute(
        """
        SELECT allowed_mode, connector, action_type
        FROM factory_policies
        WHERE status = 'active'
          AND scope_type = 'global'
          AND scope_id = ''
          AND connector IN ('', ?)
          AND action_type IN ('', ?)
        ORDER BY length(connector) DESC, length(action_type) DESC, id DESC
        LIMIT 1
        """,
        (connector or "", action_type or ""),
    ).fetchone()
    allowed_mode = row["allowed_mode"] if row and row["allowed_mode"] in MODES else "review_first"
    if _MODE_RANK[allowed_mode] >= _MODE_RANK[requested_mode]:
        return True, f"connector/action policy allows {allowed_mode}"
    return False, f"{connector or 'connector'}:{action_type or 'action'} requires {requested_mode} policy"


def set_policy(
    conn: sqlite3.Connection,
    *,
    allowed_mode: str,
    scope_type: str = "global",
    scope_id: str = "",
    connector: str = "",
    action_type: str = "",
) -> int:
    if allowed_mode not in MODES:
        raise ValueError(f"unsupported mode: {allowed_mode}")
    scope_type = scope_type or "global"
    scope_id = str(scope_id or "")
    conn.execute(
        """
        INSERT INTO factory_policies (
            scope_type, scope_id, connector, action_type, allowed_mode, status, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
        ON CONFLICT(scope_type, scope_id, connector, action_type) DO UPDATE SET
            allowed_mode=excluded.allowed_mode,
            status='active',
            updated_at=CURRENT_TIMESTAMP
        """,
        (scope_type, scope_id, connector or "", action_type or "", allowed_mode),
    )
    row = conn.execute(
        """
        SELECT id FROM factory_policies
        WHERE scope_type = ? AND scope_id = ? AND connector = ? AND action_type = ?
        """,
        (scope_type, scope_id, connector or "", action_type or ""),
    ).fetchone()
    return int(row["id"])


def _artifact(conn: sqlite3.Connection, factory_run_id: int, artifact_type: str, artifact_id: int, label: str = "") -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO factory_artifacts (factory_run_id, artifact_type, artifact_id, label)
        VALUES (?, ?, ?, ?)
        """,
        (int(factory_run_id), artifact_type, int(artifact_id), label),
    )


def _artifact_ids(conn: sqlite3.Connection, factory_run_id: int, artifact_type: str) -> list[int]:
    return [
        int(row["artifact_id"])
        for row in conn.execute(
            """
            SELECT artifact_id
            FROM factory_artifacts
            WHERE factory_run_id = ? AND artifact_type = ?
            ORDER BY id ASC
            """,
            (int(factory_run_id), artifact_type),
        ).fetchall()
    ]


def _stage(
    conn: sqlite3.Connection,
    factory_run_id: int,
    stage_name: str,
    *,
    status: str = "completed",
    role: str = "",
    agent_run_id: int | None = None,
    output: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO factory_stages (
            factory_run_id, stage_name, status, role, agent_run_id, output_json, finished_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(factory_run_id, stage_name) DO UPDATE SET
            status=excluded.status,
            role=excluded.role,
            agent_run_id=excluded.agent_run_id,
            output_json=excluded.output_json,
            finished_at=CURRENT_TIMESTAMP
        """,
        (
            int(factory_run_id),
            stage_name,
            status,
            role or None,
            int(agent_run_id) if agent_run_id is not None else None,
            json.dumps(output or {}, ensure_ascii=True),
        ),
    )


def _role_run(
    conn: sqlite3.Connection,
    *,
    role: str,
    intent: dict[str, Any],
    plan_id: int,
    retrieval_run_id: int | None,
    factory_run_id: int,
    workflow_pack: str,
) -> int:
    objective = f"factory {role}: {intent['objective']}"
    conn.execute(
        """
        INSERT INTO agent_tasks (objective, context, constraints_json, priority, status)
        VALUES (?, ?, ?, ?, 'open')
        """,
        (
            objective[:2000],
            (intent.get("context") or "")[:2000],
            json.dumps(intent.get("constraints", []), ensure_ascii=True),
            int(intent.get("priority") or 2),
        ),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    responsibility = {
        "planner": "Create a bounded plan with assumptions and validation gates.",
        "researcher": "Gather cited evidence and identify missing context.",
        "executor": "Draft candidate actions without bypassing approval.",
        "reviewer": "Check evidence, risks, rollback notes, and validation gates.",
        "critic": "Find unsafe assumptions, missing approvals, and likely failure modes.",
    }[role]
    provider_name = "local_factory"
    provider_output: dict[str, Any] = {}
    configured_backend = os.getenv("MYOS_FACTORY_ROLE_BACKEND", "").strip()
    if configured_backend:
        try:
            backend = providers.get_backend(configured_backend)
            ok, _ = backend.available()
            if ok:
                result = backend.reason(
                    conn,
                    {
                        "purpose": f"factory_{role}",
                        "objective": objective,
                        "context": intent.get("context") or "",
                        "factory": {
                            "factory_run_id": int(factory_run_id),
                            "workflow_pack": workflow_pack,
                            "intent_id": int(intent["id"]),
                            "plan_id": int(plan_id),
                            "retrieval_run_id": int(retrieval_run_id) if retrieval_run_id is not None else None,
                        },
                    },
                )
                provider_name = f"factory_{backend.name}"
                provider_output = {
                    "reply": str(result.get("reply") or "")[:2000],
                    "plan": result.get("plan") or [],
                    "actions": result.get("actions") or [],
                }
        except Exception as exc:  # noqa: BLE001 - provider roles must degrade to local fallback
            provider_output = {"provider_error": str(exc)[:500]}
    role_packet = {
        "factory_run_id": int(factory_run_id),
        "workflow_pack": workflow_pack,
        "role": role,
        "intent_id": int(intent["id"]),
        "plan_id": int(plan_id),
        "retrieval_run_id": int(retrieval_run_id) if retrieval_run_id is not None else None,
        "responsibility": responsibility,
        "approval_gate": role in {"executor", "reviewer", "critic"},
        "provider_output": provider_output,
    }
    conn.execute(
        """
        INSERT INTO agent_runs (agent_task_id, agent_name, provider, status, plan_json, summary, finished_at)
        VALUES (?, ?, ?, 'completed', ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            task_id,
            role,
            provider_name,
            json.dumps(role_packet, ensure_ascii=True),
            f"Factory {role} completed for intent #{intent['id']} plan #{plan_id}",
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _latest_plan_for_intent(conn: sqlite3.Connection, intent_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM plans
        WHERE intent_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(intent_id),),
    ).fetchone()
    return int(row["id"]) if row else None


def learning_insights(
    conn: sqlite3.Connection,
    *,
    intent_id: int | None = None,
    workflow_pack: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: list[object] = []
    if intent_id is not None:
        clauses.append("r.intent_id = ?")
        params.append(int(intent_id))
    if workflow_pack:
        clauses.append("r.workflow_pack = ?")
        params.append(workflow_pack)
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT l.outcome, l.notes, l.retrospective_json, r.workflow_pack, r.mode
        FROM factory_learning l
        JOIN factory_runs r ON r.id = l.factory_run_id
        WHERE {' AND '.join(clauses)}
        ORDER BY l.id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    outcomes: dict[str, int] = {}
    blockers: dict[str, int] = {}
    side_effects: dict[str, int] = {}
    useful_sources: dict[str, int] = {}
    notes: list[str] = []
    for row in rows:
        outcome = str(row["outcome"])
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        note = str(row["notes"] or "").strip()
        if note:
            notes.append(note[:200])
        try:
            retro = json.loads(row["retrospective_json"] or "{}")
        except (TypeError, ValueError):
            retro = {}
        receipt_side_effects = retro.get("receipt_side_effects") or {}
        if not isinstance(receipt_side_effects, dict):
            receipt_side_effects = {}
        for side_effect, count in receipt_side_effects.items():
            key = str(side_effect)
            side_effects[key] = side_effects.get(key, 0) + int(count or 0)
        for receipt in retro.get("recent_receipts") or []:
            status = str(receipt.get("final_status") or "")
            if status in {"blocked", "failed"}:
                blockers[status] = blockers.get(status, 0) + 1
            if not receipt_side_effects:
                for side_effect in receipt.get("side_effects") or []:
                    key = str(side_effect)
                    side_effects[key] = side_effects.get(key, 0) + 1
        for artifact_type in retro.get("useful_artifacts") or []:
            useful_sources[str(artifact_type)] = useful_sources.get(str(artifact_type), 0) + 1
    return {
        "count": len(rows),
        "outcomes": outcomes,
        "blockers": blockers,
        "side_effects": side_effects,
        "useful_sources": useful_sources,
        "notes": notes[:5],
    }


def _apply_learning_to_plan(
    conn: sqlite3.Connection,
    *,
    intent_id: int,
    plan_id: int,
    workflow_pack: str,
) -> dict[str, Any]:
    insights = learning_insights(conn, intent_id=intent_id, workflow_pack=workflow_pack)
    if not insights["count"]:
        insights = learning_insights(conn, workflow_pack=workflow_pack)
    if not insights["count"]:
        return insights
    note = "; ".join(insights["notes"][:2]) or "prior factory run found follow-up learning"
    exists = conn.execute(
        """
        SELECT 1 FROM plan_validations
        WHERE plan_id = ? AND check_name = 'factory_learning_review'
        LIMIT 1
        """,
        (int(plan_id),),
    ).fetchone()
    if not exists:
        conn.execute(
            """
            INSERT INTO plan_validations (plan_id, check_name, command, expected)
            VALUES (?, 'factory_learning_review', ?, ?)
            """,
            (
                int(plan_id),
                f"myos factory insights --intent {intent_id}",
                "review prior learning before execution",
            ),
        )
    if insights["blockers"]:
        conn.execute(
            """
            INSERT INTO plan_risks (plan_id, risk, mitigation, severity)
            VALUES (?, ?, ?, 'medium')
            """,
            (
                int(plan_id),
                f"Prior factory blockers observed: {json.dumps(insights['blockers'], ensure_ascii=True)}",
                f"Reviewer and critic must check prior learning: {note}",
            ),
        )
    return insights


def start_review_first_run(
    conn: sqlite3.Connection,
    *,
    intent_id: int,
    mode: str = "review_first",
    workflow_pack: str = "intent_execution",
    executor_backend: str = "local",
    executor_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"unsupported factory mode: {mode}")
    if workflow_pack not in WORKFLOW_PACKS:
        raise ValueError(f"unsupported workflow pack: {workflow_pack}")
    executor_backend = (executor_backend or "local").strip().lower()
    if executor_backend not in {"local", "zero"}:
        raise ValueError(f"unsupported executor backend: {executor_backend}")
    if executor_backend != "local" and workflow_pack != "software_delivery":
        raise ValueError("external coding executors are only supported for software_delivery runs")
    intent = intents.get_intent(conn, int(intent_id))
    if intent is None:
        raise ValueError(f"intent #{intent_id} not found")
    allowed, reason = mode_allowed(conn, intent_id=int(intent_id), requested_mode=mode)
    if not allowed:
        raise ValueError(reason)

    plan_id = _latest_plan_for_intent(conn, int(intent_id)) or plans.create_plan(conn, intent_id=int(intent_id))
    learning = _apply_learning_to_plan(conn, intent_id=int(intent_id), plan_id=int(plan_id), workflow_pack=workflow_pack)
    conn.execute(
        """
        INSERT INTO factory_runs (
            intent_id, plan_id, mode, workflow_pack, executor_backend, executor_context_json, status, summary
        )
        VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
        """,
        (
            int(intent_id),
            int(plan_id),
            mode,
            workflow_pack,
            executor_backend,
            json.dumps(executor_context or {}, ensure_ascii=True),
            f"{workflow_pack} factory run for intent #{intent_id}",
        ),
    )
    factory_run_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    _artifact(conn, factory_run_id, "plan", plan_id, "factory plan")

    hits = graphrag.retrieve(
        conn,
        str(intent["objective"]),
        limit=5,
        record_run=True,
        mode=f"factory_{workflow_pack}",
    )
    retrieval_run_id = None
    if hits and hits[0].get("retrieval_run_id"):
        retrieval_run_id = int(hits[0]["retrieval_run_id"])
    else:
        row = conn.execute(
            "SELECT id FROM retrieval_runs WHERE query = ? ORDER BY id DESC LIMIT 1",
            (str(intent["objective"]),),
        ).fetchone()
        retrieval_run_id = int(row["id"]) if row else None
    if retrieval_run_id is not None:
        _artifact(conn, factory_run_id, "retrieval_run", retrieval_run_id, "factory context")
        evidence_id = plans.attach_retrieval_run_evidence(
            conn,
            intent_id=int(intent_id),
            retrieval_run_id=int(retrieval_run_id),
        )
        _artifact(conn, factory_run_id, "intent_evidence", evidence_id, "retrieval evidence")
    _stage(
        conn,
        factory_run_id,
        "context",
        output={"retrieval_run_id": retrieval_run_id, "hits": len(hits)},
    )

    review_packet_id = plans.create_review_packet(
        conn,
        plan_id=plan_id,
        retrieval_run_id=retrieval_run_id,
    )
    _artifact(conn, factory_run_id, "review_packet", review_packet_id, "review packet")

    agent_run_ids: list[int] = []
    for role in ("planner", "researcher", "executor", "reviewer", "critic"):
        agent_run_id = _role_run(
            conn,
            role=role,
            intent=intent,
            plan_id=plan_id,
            retrieval_run_id=retrieval_run_id,
            factory_run_id=factory_run_id,
            workflow_pack=workflow_pack,
        )
        agent_run_ids.append(agent_run_id)
        _artifact(conn, factory_run_id, "agent_run", agent_run_id, role)
        _stage(
            conn,
            factory_run_id,
            role,
            role=role,
            agent_run_id=agent_run_id,
            output={"agent_run_id": agent_run_id},
        )
    prepared_action_ids: list[int] = []
    if mode == "review_first":
        if workflow_pack == "software_delivery" and executor_backend == "zero":
            prepared_action_ids = prepare_execution_actions(conn, factory_run_id)
        _stage(
            conn,
            factory_run_id,
            "approval",
            status="waiting",
            output={
                "reason": "review-first factory run stops before approved patch application",
                "prepared_action_ids": prepared_action_ids,
            },
        )
        _stage(
            conn,
            factory_run_id,
            "execution",
            status="blocked",
            output={"reason": "approval required", "prepared_action_ids": prepared_action_ids},
        )
        final_status = "awaiting_approval"
        if prepared_action_ids:
            summary = (
                f"Factory run #{factory_run_id} prepared {len(prepared_action_ids)} "
                "approval-gated Zero action(s) and stopped before patch application."
            )
        else:
            summary = f"Factory run #{factory_run_id} completed review-first stages and stopped before execution."
    else:
        execution = advance_execution(conn, factory_run_id)
        prepared_action_ids = [
            int(item["action_id"])
            for item in execution.get("results", [])
            if item.get("action_id") is not None
        ]
        _stage(
            conn,
            factory_run_id,
            "approval",
            status="completed" if mode == "full_autonomous" else "waiting",
            output={"reason": f"{mode} policy evaluated", "executed": execution["executed"]},
        )
        final_status = "execution_ready" if execution["pending"] else "execution_completed"
        summary = (
            f"Factory run #{factory_run_id} prepared {execution['actions']} action(s), "
            f"executed={execution['executed']}, pending={execution['pending']}."
        )
    _stage(conn, factory_run_id, "learning", status="pending", output={"reason": "waiting for outcome"})
    conn.execute(
        """
        UPDATE factory_runs
        SET status=?,
            summary=?,
            finished_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (
            final_status,
            summary,
            factory_run_id,
        ),
    )
    append_event(
        conn,
        "factory_run_created",
        "factory_run",
        factory_run_id,
        json.dumps(
            {
                "intent_id": int(intent_id),
                "mode": mode,
                "workflow_pack": workflow_pack,
                "executor_backend": executor_backend,
            },
            ensure_ascii=True,
        ),
    )
    observability.link_current_trace(conn, factory_run_id=factory_run_id)
    return {
        "id": factory_run_id,
        "intent_id": int(intent_id),
        "plan_id": int(plan_id),
        "retrieval_run_id": retrieval_run_id,
        "review_packet_id": review_packet_id,
        "agent_run_ids": _artifact_ids(conn, factory_run_id, "agent_run") or agent_run_ids,
        "proposed_action_ids": prepared_action_ids,
        "status": final_status,
        "executor_backend": executor_backend,
        "learning_insights": learning,
    }


def _connector_specs_from_evidence(conn: sqlite3.Connection, intent: dict[str, Any], objective: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT x.connector, x.external_id, x.item_type, x.title, x.url
        FROM intent_evidence e
        JOIN external_items x ON x.id = CAST(e.source_id AS INTEGER)
        WHERE e.intent_id = ? AND e.source_type = 'external_item'
        ORDER BY e.confidence DESC, e.id ASC
        LIMIT 4
        """,
        (int(intent["id"]),),
    ).fetchall()
    specs: list[dict[str, Any]] = []
    for row in rows:
        connector = str(row["connector"])
        target_ref = str(row["external_id"] or "draft")
        specs.append(
            {
                "action_type": "draft_external_update",
                "title": f"Draft {connector} update for intent #{intent['id']}",
                "payload": {
                    "target": connector,
                    "connector": connector,
                    "operation": "comment",
                    "target_ref": target_ref,
                    "draft": f"Factory update for {connector}:{target_ref}: {objective}",
                    "url": row["url"] or "",
                    "rollback_note": "Post a correction or remove the drafted/sent connector update if possible.",
                    "dry_run": True,
                },
                "requires_approval": 1,
                "connector": connector,
            }
        )
    return specs


def _factory_executor_context(run: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(run.get("executor_context_json") or "{}")
    except (TypeError, ValueError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _git_root(cwd: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _review_packet_id_for_run(conn: sqlite3.Connection, factory_run_id: int) -> int | None:
    ids = _artifact_ids(conn, int(factory_run_id), "review_packet")
    return ids[0] if ids else None


def _zero_signal_text(conn: sqlite3.Connection, value: object, *, limit: int = 500) -> str:
    return apply_privacy_filters(conn, str(value or "").strip().replace("\n", " "))[:limit]


def _zero_error_signals(conn: sqlite3.Connection, errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for error in errors[:5]:
        if not isinstance(error, dict):
            continue
        signals.append(
            {
                "code": _zero_signal_text(conn, error.get("code") or "error", limit=80),
                "message": _zero_signal_text(conn, error.get("message") or ""),
                "recoverable": error.get("recoverable"),
            }
        )
    return signals


def _zero_action_metadata(
    conn: sqlite3.Connection,
    result: zero_executor.ZeroRunResult,
    *,
    changed_files: list[str],
) -> dict[str, Any]:
    stderr_text = result.stderr or ""
    stderr_bytes = len(stderr_text.encode("utf-8"))
    return {
        "schema": "myos.zero_executor.action_metadata.v1",
        "stream_schema_version": zero_executor.SCHEMA_VERSION,
        "status": result.status,
        "exit_code": result.exit_code,
        "timed_out": bool(result.timed_out),
        "run_id": result.run_id,
        "session_id": result.session_id,
        "provider": result.provider,
        "model": result.model,
        "api_model": result.api_model,
        "event_counts": result.event_counts,
        "changed_files": changed_files,
        "permission_events_count": len(result.permission_events),
        "warnings": [_zero_signal_text(conn, warning) for warning in result.warnings if warning][:5],
        "errors": _zero_error_signals(conn, result.errors),
        "usage": result.usage,
        "protocol_errors": [_zero_signal_text(conn, error) for error in result.protocol_errors[:5]],
        "final_text": _zero_signal_text(conn, result.final_text or "", limit=1000),
        "stderr_preview": _zero_signal_text(conn, stderr_text, limit=1000),
        "stderr_bytes": stderr_bytes,
        "stderr_truncated": stderr_bytes > 1000,
    }


def _prepare_zero_software_action(
    conn: sqlite3.Connection,
    *,
    run: dict[str, Any],
    intent: dict[str, Any],
    task_id: int,
) -> tuple[int, int]:
    context = _factory_executor_context(run)
    repo = str(context.get("repo") or ".")
    root = _git_root(repo)
    if not root:
        raise ValueError(f"zero executor requires a git repo: {repo}")
    # Precedence: explicit factory context > MYOS_ZERO_TIMEOUT_SECONDS env override
    # > module default. The env var lets operators cap all Zero runs globally
    # without editing call sites, matching the approval-TTL env pattern.
    timeout_env = os.getenv("MYOS_ZERO_TIMEOUT_SECONDS", "").strip()
    env_timeout: int | None = None
    if timeout_env:
        try:
            env_timeout = max(1, int(timeout_env))
        except ValueError:
            env_timeout = None
    timeout = int(context.get("timeout") or env_timeout or zero_executor.DEFAULT_TIMEOUT)
    max_turns = int(context.get("max_turns") or 0) or None
    verification_commands = [
        str(command).strip()
        for command in (context.get("verification_commands") or [])
        if str(command).strip()
    ]
    verification_block = ""
    if verification_commands:
        verification_block = "\nSuggested verification commands:\n" + "\n".join(f"- {command}" for command in verification_commands)
    objective = (
        f"{intent['objective']}\n\n"
        "Leave changes uncommitted. Run relevant local verification if safe."
        f"{verification_block}"
    )
    retry_parts = [
        "myos",
        "factory",
        "start",
        "--intent",
        str(intent["id"]),
        "--mode",
        str(run.get("mode") or "review_first"),
        "--pack",
        "software_delivery",
        "--executor",
        "zero",
        "--repo",
        root,
        "--timeout",
        str(timeout),
    ]
    if max_turns is not None:
        retry_parts.extend(["--max-turns", str(max_turns)])
    for command in verification_commands:
        retry_parts.extend(["--verify-command", command])
    retry_command = " ".join(shlex.quote(part) for part in retry_parts)
    worktree = tempfile.mkdtemp(prefix="myos-zero-wt-")
    try:
        subprocess.run(
            ["git", "-C", root, "worktree", "add", "--detach", worktree],
            capture_output=True,
            text=True,
            check=True,
        )
        result = zero_executor.run_zero_stream(
            objective,
            cwd=worktree,
            timeout=timeout,
            max_turns=max_turns,
        )
        subprocess.run(["git", "-C", worktree, "add", "-A"], capture_output=True, text=True, check=False)
        diff = subprocess.run(
            ["git", "-C", worktree, "diff", "--cached"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        diff_changed_files = [
            line.strip()
            for line in subprocess.run(
                ["git", "-C", worktree, "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.splitlines()
            if line.strip()
        ]
        changed_files = diff_changed_files or result.changed_files
        numstat = subprocess.run(
            ["git", "-C", worktree, "diff", "--cached", "--numstat"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines()
        diff_stats = {"files": 0, "additions": 0, "deletions": 0, "binary_files": 0}
        for line in numstat:
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            diff_stats["files"] += 1
            if parts[0] == "-" or parts[1] == "-":
                diff_stats["binary_files"] += 1
                continue
            try:
                diff_stats["additions"] += int(parts[0])
                diff_stats["deletions"] += int(parts[1])
            except ValueError:
                diff_stats["binary_files"] += 1
        agent_run_id = zero_executor.record_zero_agent_run(
            conn,
            task_id=int(task_id),
            result=result,
            factory_run_id=int(run["id"]),
        )
        payload = {
            "agent": "zero",
            "task": str(intent["objective"]),
            "repo_root": root,
            "factory_run_id": int(run["id"]),
            "zero": _zero_action_metadata(conn, result, changed_files=changed_files),
            "verification_commands": verification_commands,
            "rollback_note": "Reject or revert the proposed patch before applying it to the source repository.",
        }
        diff_too_large = len(diff) > _MAX_ZERO_PATCH_BYTES
        if diff.strip() and not diff_too_large:
            action_type = "apply_patch"
            title = f"Apply Zero patch for intent #{intent['id']}"
            payload["diff"] = diff
            payload["changed_files"] = changed_files
            payload["diff_stats"] = diff_stats
        else:
            action_type = "draft_external_update"
            title = (
                f"Review oversized Zero patch for intent #{intent['id']}"
                if diff_too_large
                else f"Review Zero output for intent #{intent['id']}"
            )
            draft = result.final_text or result.stderr or "Zero produced no code changes."
            if diff_too_large:
                draft = (
                    f"Zero produced a {len(diff)} byte diff, above the MYOS approval patch limit "
                    f"of {_MAX_ZERO_PATCH_BYTES} bytes. Review the changed files and rerun with a "
                    "smaller scope before applying."
                )
            payload.update(
                {
                    "target": "outbox",
                    "draft": draft[:8000],
                    "changed_files": changed_files,
                    "diff_stats": diff_stats,
                    "diff_too_large": diff_too_large,
                    "diff_bytes": len(diff),
                    "diff_limit_bytes": _MAX_ZERO_PATCH_BYTES,
                }
            )
        action_id = agentcore.enqueue_proposal(
            conn,
            task_id=int(task_id),
            action_type=action_type,
            title=title,
            payload=payload,
            requires_approval=1,
        )
        follow_up_id = None
        if not result.terminal_ok():
            follow_up_text = (
                f"Follow up on Zero executor {result.status} for factory run #{run['id']}: "
                f"{intent['objective']}"
            )
            follow_up_id = insert_inbox_item_dedup(
                conn,
                text=follow_up_text,
                kind="task",
                owner=None,
                due_date=None,
                confidence=0.85,
                source="zero_executor",
            )
            if follow_up_id is not None:
                _artifact(conn, int(run["id"]), "inbox_item", follow_up_id, "zero follow-up")
                append_event(
                    conn,
                    "zero_executor_follow_up_created",
                    "inbox_item",
                    follow_up_id,
                    json.dumps(
                        {
                            "factory_run_id": int(run["id"]),
                            "agent_run_id": int(agent_run_id),
                            "agent_action_id": int(action_id),
                            "status": result.status,
                            "exit_code": result.exit_code,
                        },
                        ensure_ascii=True,
                    ),
                )
        packet_id = _review_packet_id_for_run(conn, int(run["id"]))
        if packet_id is not None:
            plans.attach_executor_artifact(
                conn,
                packet_id=int(packet_id),
                artifact={
                    "type": "zero_executor",
                    "agent_run_id": int(agent_run_id),
                    "agent_action_id": int(action_id),
                    "action_type": action_type,
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "timed_out": bool(result.timed_out),
                    "timeout_seconds": int(timeout),
                    "changed_files": changed_files,
                    "diff_stats": diff_stats,
                    "diff_too_large": diff_too_large,
                    "diff_bytes": len(diff),
                    "diff_limit_bytes": _MAX_ZERO_PATCH_BYTES,
                    "run_id": result.run_id,
                    "session_id": result.session_id,
                    "executor_isolated_worktree": True,
                    "executor_worktree_retained": False,
                    "permission_events_count": len(result.permission_events),
                    "warnings": [_zero_signal_text(conn, warning) for warning in result.warnings if warning][:5],
                    "errors": _zero_error_signals(conn, result.errors),
                    "protocol_errors": [_zero_signal_text(conn, error) for error in result.protocol_errors[:5]],
                    "verification_commands": verification_commands,
                    "summary": _zero_signal_text(conn, result.final_text or "", limit=1000),
                    "stderr_bytes": len((result.stderr or "").encode("utf-8")),
                    "stderr_truncated": len((result.stderr or "").encode("utf-8")) > 1000,
                    "approval_command": f"myos approve --action {action_id} --execute",
                    "retry_command": retry_command,
                    "follow_up_inbox_id": int(follow_up_id) if follow_up_id is not None else None,
                },
            )
        return action_id, agent_run_id
    finally:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", worktree], capture_output=True, text=True, check=False)
        shutil.rmtree(worktree, ignore_errors=True)


def _execution_action_specs(conn: sqlite3.Connection, run: dict[str, Any], intent: dict[str, Any]) -> list[dict[str, Any]]:
    objective = str(intent["objective"])
    base = [
        {
            "action_type": "create_inbox_item",
            "title": f"Track factory follow-up for intent #{intent['id']}",
            "payload": {
                "text": f"Factory follow-up: {objective}",
                "kind": "task",
                "source": f"factory_run:{run['id']}",
                "rollback_note": "Archive or complete the created inbox item if it is no longer needed.",
            },
            "requires_approval": 0,
            "connector": "",
        }
    ]
    pack = str(run.get("workflow_pack") or "intent_execution")
    if pack in {"connector_ops", "daily_ops"}:
        connector_specs = _connector_specs_from_evidence(conn, intent, objective)
        base.extend(
            connector_specs
            or [
                {
                    "action_type": "draft_external_update",
                    "title": f"Draft connector update for intent #{intent['id']}",
                    "payload": {
                        "target": "jira",
                        "connector": "jira",
                        "operation": "draft_note",
                        "target_ref": "draft",
                        "draft": f"Draft update for intent #{intent['id']}: {objective}",
                        "rollback_note": "Post a clarifying correction or remove the draft if sent incorrectly.",
                        "dry_run": True,
                    },
                    "requires_approval": 1,
                    "connector": "jira",
                }
            ]
        )
    elif pack == "software_delivery":
        base.append(
            {
                "action_type": "draft_external_update",
                "title": f"Draft software delivery review note for intent #{intent['id']}",
                "payload": {
                    "target": "github",
                    "connector": "github",
                    "operation": "draft_note",
                    "target_ref": "draft",
                    "draft": f"Software delivery review note for intent #{intent['id']}: {objective}",
                    "rollback_note": "Replace the draft with a corrected review note before sending.",
                    "dry_run": True,
                },
                "requires_approval": 1,
                "connector": "github",
            }
        )
    return base


def prepare_execution_actions(conn: sqlite3.Connection, factory_run_id: int) -> list[int]:
    existing = _artifact_ids(conn, int(factory_run_id), "agent_action")
    if existing:
        return existing
    run = get_factory_run(conn, int(factory_run_id))
    if run is None:
        raise ValueError(f"factory run #{factory_run_id} not found")
    intent = intents.get_intent(conn, int(run["intent_id"]))
    if intent is None:
        raise ValueError(f"intent #{run['intent_id']} not found")
    conn.execute(
        """
        INSERT INTO agent_tasks (objective, context, constraints_json, priority, status)
        VALUES (?, ?, ?, ?, 'open')
        """,
        (
            f"factory execution: {intent['objective']}"[:2000],
            (intent.get("context") or "")[:2000],
            json.dumps(
                {
                    "source": "factory",
                    "factory_run_id": int(factory_run_id),
                    "mode": run["mode"],
                    "workflow_pack": run["workflow_pack"],
                },
                ensure_ascii=True,
            ),
            int(intent.get("priority") or 2),
        ),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    _artifact(conn, int(factory_run_id), "agent_task", task_id, "execution task")
    if str(run.get("workflow_pack") or "") == "software_delivery" and str(run.get("executor_backend") or "local") == "zero":
        action_id, agent_run_id = _prepare_zero_software_action(conn, run=run, intent=intent, task_id=task_id)
        _artifact(conn, int(factory_run_id), "agent_run", agent_run_id, "zero executor")
        _artifact(conn, int(factory_run_id), "agent_action", action_id, "zero apply_patch")
        append_event(
            conn,
            "factory_actions_prepared",
            "factory_run",
            int(factory_run_id),
            json.dumps({"actions": [action_id], "executor_backend": "zero"}, ensure_ascii=True),
        )
        return [action_id]
    action_ids: list[int] = []
    for spec in _execution_action_specs(conn, run, intent):
        action_id = agentcore.enqueue_proposal(
            conn,
            task_id=task_id,
            action_type=spec["action_type"],
            title=spec["title"],
            payload=spec["payload"],
            requires_approval=int(spec["requires_approval"]),
        )
        action_ids.append(action_id)
        _artifact(conn, int(factory_run_id), "agent_action", action_id, spec["action_type"])
    append_event(
        conn,
        "factory_actions_prepared",
        "factory_run",
        int(factory_run_id),
        json.dumps({"actions": action_ids}, ensure_ascii=True),
    )
    return action_ids


def _action_policy_allows(conn: sqlite3.Connection, run: dict[str, Any], action: sqlite3.Row) -> tuple[bool, str]:
    payload = json.loads(action["payload_json"] or "{}")
    action_type = str(action["action_type"])
    connector = str(payload.get("connector") or payload.get("target") or "")
    verdict = autonomy.classify_action(action_type, payload, level=autonomy.level_from_policy(conn))
    if verdict["tier"] == autonomy.BLOCKED:
        return False, str(verdict["reason"])
    if action_type in autonomy.AUTO_ACTION_TYPES:
        return True, "safe local action"
    mode = str(run["mode"])
    if mode == "review_first":
        return False, "review_first requires explicit approval"
    if mode == "semi_autonomous":
        return False, "semi_autonomous leaves non-local actions approval-gated"
    allowed, reason = connector_action_allowed(
        conn,
        requested_mode="full_autonomous",
        connector=connector,
        action_type=action_type,
    )
    return allowed, reason


def _receipt_ids_for_actions(conn: sqlite3.Connection, action_ids: list[int]) -> list[int]:
    if not action_ids:
        return []
    placeholders = ",".join("?" for _ in action_ids)
    return [
        int(row["id"])
        for row in conn.execute(
            f"""
            SELECT id
            FROM action_execution_receipts
            WHERE agent_action_id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(action_ids),
        ).fetchall()
    ]


def advance_execution(conn: sqlite3.Connection, factory_run_id: int, *, approve: bool = False) -> dict[str, Any]:
    run = get_factory_run(conn, int(factory_run_id))
    if run is None:
        raise ValueError(f"factory run #{factory_run_id} not found")
    action_ids = prepare_execution_actions(conn, int(factory_run_id))
    if str(run["mode"]) == "full_autonomous" and len(action_ids) > _MAX_FULL_AUTO_ACTIONS:
        raise ValueError(f"full_autonomous action limit exceeded: {len(action_ids)} > {_MAX_FULL_AUTO_ACTIONS}")
    executed = 0
    pending = 0
    blocked = 0
    results: list[dict[str, Any]] = []
    for action_id in action_ids:
        action = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (int(action_id),)).fetchone()
        if not action:
            continue
        allowed, reason = _action_policy_allows(conn, run, action)
        should_execute = allowed or approve
        if not should_execute:
            pending += 1
            results.append({"action_id": int(action_id), "status": "pending", "reason": reason})
            continue
        res = approve_and_execute(
            conn,
            int(action_id),
            do_approve=bool(approve or str(run["mode"]) == "full_autonomous"),
            execute=True,
        )
        if res.get("code") == "executed":
            executed += 1
        elif res.get("status") in {"blocked", "failed"}:
            blocked += 1
        else:
            pending += 1
        results.append({"action_id": int(action_id), "status": res.get("status"), "result": res.get("result")})
    receipt_ids = _receipt_ids_for_actions(conn, action_ids)
    for receipt_id in receipt_ids:
        _artifact(conn, int(factory_run_id), "execution_receipt", receipt_id, "execution receipt")
    status = "completed" if pending == 0 and blocked == 0 else "waiting" if pending else "blocked"
    _stage(
        conn,
        int(factory_run_id),
        "execution",
        status=status,
        output={"actions": len(action_ids), "executed": executed, "pending": pending, "blocked": blocked, "results": results},
    )
    conn.execute(
        """
        UPDATE factory_runs
        SET status=?, summary=?
        WHERE id=?
        """,
        (
            "execution_completed" if status == "completed" else "execution_ready",
            f"Factory execution actions={len(action_ids)} executed={executed} pending={pending} blocked={blocked}",
            int(factory_run_id),
        ),
    )
    return {"actions": len(action_ids), "executed": executed, "pending": pending, "blocked": blocked, "results": results}


def record_stage(
    conn: sqlite3.Connection,
    *,
    factory_run_id: int,
    stage_name: str,
    status: str = "completed",
    note: str = "",
) -> None:
    if stage_name not in STAGES:
        raise ValueError(f"unsupported factory stage: {stage_name}")
    run = get_factory_run(conn, int(factory_run_id))
    if run is None:
        raise ValueError(f"factory run #{factory_run_id} not found")
    _stage(conn, int(factory_run_id), stage_name, status=status, output={"note": note})


def get_factory_run(conn: sqlite3.Connection, factory_run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM factory_runs WHERE id = ?", (int(factory_run_id),)).fetchone()
    if not row:
        return None
    run = dict(row)
    run["stages"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT stage_name, status, role, agent_run_id, output_json, started_at, finished_at
            FROM factory_stages
            WHERE factory_run_id = ?
            ORDER BY id ASC
            """,
            (int(factory_run_id),),
        ).fetchall()
    ]
    run["artifacts"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT artifact_type, artifact_id, label, created_at
            FROM factory_artifacts
            WHERE factory_run_id = ?
            ORDER BY id ASC
            """,
            (int(factory_run_id),),
        ).fetchall()
    ]
    return run


def _receipt_approval_context(request_json: str | None) -> dict[str, Any]:
    try:
        request = json.loads(request_json or "{}")
    except (TypeError, ValueError):
        request = {}
    context = request.get("approval_context") if isinstance(request, dict) else {}
    return context if isinstance(context, dict) else {}


def learn(
    conn: sqlite3.Connection,
    *,
    factory_run_id: int,
    outcome: str,
    notes: str = "",
) -> int:
    if outcome not in {"success", "partial", "failed"}:
        raise ValueError(f"unsupported outcome: {outcome}")
    run = get_factory_run(conn, int(factory_run_id))
    if run is None:
        raise ValueError(f"factory run #{factory_run_id} not found")
    receipts: list[dict[str, Any]] = []
    for r in conn.execute(
        """
        SELECT action_type, final_status, follow_up_required, follow_up_inbox_id, request_json
        FROM action_execution_receipts
        ORDER BY created_at DESC
        LIMIT 20
        """
    ).fetchall():
        context = _receipt_approval_context(r["request_json"])
        side_effects = context.get("side_effects") or []
        if not isinstance(side_effects, list):
            side_effects = []
        receipts.append(
            {
                "action_type": str(r["action_type"]),
                "final_status": str(r["final_status"]),
                "follow_up_required": bool(r["follow_up_required"]),
                "follow_up_inbox_id": int(r["follow_up_inbox_id"]) if r["follow_up_inbox_id"] else None,
                "side_effects": [str(side_effect) for side_effect in side_effects if side_effect],
                "dry_run": bool(context.get("dry_run")),
                "approval_reason": str(context.get("approval_reason") or ""),
            }
        )
    receipt_side_effects: dict[str, int] = {}
    for receipt in receipts:
        for side_effect in receipt["side_effects"]:
            receipt_side_effects[side_effect] = receipt_side_effects.get(side_effect, 0) + 1
    safe_notes = apply_privacy_filters(conn, notes or "")
    retrospective = {
        "factory_run_id": int(factory_run_id),
        "outcome": outcome,
        "notes": safe_notes,
        "stage_count": len(run["stages"]),
        "artifact_count": len(run["artifacts"]),
        "useful_artifacts": sorted({str(a["artifact_type"]) for a in run["artifacts"]}),
        "stage_statuses": {str(s["stage_name"]): str(s["status"]) for s in run["stages"]},
        "recent_receipts": receipts,
        "receipt_side_effects": receipt_side_effects,
    }
    conn.execute(
        """
        INSERT INTO factory_learning (factory_run_id, outcome, notes, retrospective_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            int(factory_run_id),
            outcome,
            safe_notes,
            json.dumps(retrospective, ensure_ascii=True),
        ),
    )
    learning_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        """
        UPDATE factory_runs
        SET outcome=?, outcome_notes=?, status='learned'
        WHERE id=?
        """,
        (outcome, safe_notes, int(factory_run_id)),
    )
    _stage(conn, int(factory_run_id), "learning", status="completed", output={"learning_id": learning_id, "outcome": outcome})
    append_event(
        conn,
        "factory_learning_recorded",
        "factory_run",
        int(factory_run_id),
        json.dumps({"learning_id": learning_id, "outcome": outcome}, ensure_ascii=True),
    )
    return learning_id


def latest_retrospective(conn: sqlite3.Connection, factory_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, outcome, notes, retrospective_json, created_at
        FROM factory_learning
        WHERE factory_run_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(factory_run_id),),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["retrospective"] = json.loads(result.get("retrospective_json") or "{}")
    except (TypeError, ValueError):
        result["retrospective"] = {}
    return result


def proactive_step(
    conn: sqlite3.Connection,
    *,
    mode: str = "review_first",
    workflow_pack: str = "daily_ops",
) -> dict[str, Any]:
    active = conn.execute(
        """
        SELECT id
        FROM factory_runs
        WHERE status IN ('running', 'awaiting_approval', 'execution_ready', 'approved_for_execution')
        ORDER BY started_at ASC, id ASC
        LIMIT 1
        """
    ).fetchone()
    if active:
        run_id = int(active["id"])
        run = get_factory_run(conn, run_id)
        waiting_execution = any(s["stage_name"] == "execution" and s["status"] == "waiting" for s in run["stages"]) if run else False
        if waiting_execution:
            result = advance_execution(conn, run_id)
            return {"action": "continued", "factory_run_id": run_id, **result}
        return {"action": "waiting", "factory_run_id": run_id}

    intent = conn.execute(
        """
        SELECT id
        FROM intents
        WHERE status = 'open'
          AND id NOT IN (
            SELECT intent_id
            FROM factory_runs
            WHERE started_at >= datetime('now', '-1 day')
          )
        ORDER BY priority ASC, created_at ASC, id ASC
        LIMIT 1
        """
    ).fetchone()
    if not intent:
        return {"action": "none", "factory_run_id": None}
    result = start_review_first_run(
        conn,
        intent_id=int(intent["id"]),
        mode=mode,
        workflow_pack=workflow_pack,
    )
    append_event(
        conn,
        "factory_proactive_step",
        "factory_run",
        int(result["id"]),
        json.dumps({"mode": mode, "workflow_pack": workflow_pack}, ensure_ascii=True),
    )
    return {"action": "started", "factory_run_id": int(result["id"]), "status": result["status"]}
