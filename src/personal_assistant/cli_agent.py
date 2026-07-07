from __future__ import annotations

import argparse
import json

from . import assistant, intents, plans
from .approval_context import format_action_review_context, format_compact_action_review_context
from .db import append_event, get_connection
from .execution import (
    _outbox_write,
    _post_github_comment,
    _post_jira_comment,
    _provider_body,
    _provider_target_summary,
    _read_provider_stdin,
    approve_and_execute,
    execute_connector_mutation,
)
from .planner import _agent_analogies, _ai_reason_artifacts
from .privacy import apply_privacy_filters, redact_obj


def _print_zero_approval_context(payload: dict) -> None:
    if not isinstance(payload, dict) or not (payload.get("zero") or payload.get("agent") == "zero"):
        return
    zero = payload.get("zero") if isinstance(payload.get("zero"), dict) else {}
    status = str(zero.get("status") or "unknown")
    exit_code = zero.get("exit_code")
    run_id = str(zero.get("run_id") or "").strip()
    session_id = str(zero.get("session_id") or "").strip()
    ref = f" run={run_id}" if run_id else ""
    if session_id:
        ref += f" session={session_id}"
    exit_part = f" exit_code={exit_code}" if exit_code is not None else ""
    print(f"  zero: status={status}{exit_part}{ref}")
    changed_files = [str(item) for item in payload.get("changed_files") or zero.get("changed_files") or [] if str(item)]
    if changed_files:
        print("  zero_changed_files: " + ",".join(changed_files[:20]))
    diff_stats = payload.get("diff_stats") if isinstance(payload.get("diff_stats"), dict) else {}
    if diff_stats:
        print(
            "  zero_diff_stats: "
            f"files={int(diff_stats.get('files') or 0)} "
            f"additions={int(diff_stats.get('additions') or 0)} "
            f"deletions={int(diff_stats.get('deletions') or 0)} "
            f"binary={int(diff_stats.get('binary_files') or 0)}"
        )
    if payload.get("diff_too_large"):
        print(
            "  zero_diff_notice: "
            f"oversized bytes={int(payload.get('diff_bytes') or 0)} "
            f"limit={int(payload.get('diff_limit_bytes') or 0)}"
        )
    for command in [str(item).strip() for item in payload.get("verification_commands") or [] if str(item).strip()][:5]:
        print(f"  zero_verify: {command}")


def _receipt_verification_lines(request: dict) -> list[str]:
    verification = request.get("verification") if isinstance(request, dict) else None
    if not isinstance(verification, dict):
        return []
    commands = [str(item).strip() for item in verification.get("commands") or [] if str(item).strip()]
    if not commands:
        return []
    status = str(verification.get("status") or "not_run")
    lines = [f"verification: {status}"]
    for command in commands[:5]:
        lines.append(f"verification_command: {command}")
    reason = str(verification.get("reason") or "").strip()
    if reason:
        lines.append(f"verification_reason: {reason}")
    return lines


def _receipt_integrity_lines(request: dict) -> list[str]:
    integrity = request.get("approval_integrity") if isinstance(request, dict) else None
    if not isinstance(integrity, dict):
        return []
    ok = bool(integrity.get("ok", False))
    verified = bool(integrity.get("payload_hash_verified", False))
    lines = [f"approval_integrity: {'ok' if ok else 'blocked'} payload_hash_verified={str(verified).lower()}"]
    reason = str(integrity.get("reason") or "").strip()
    if reason:
        lines.append(f"approval_integrity_reason: {reason}")
    age = integrity.get("approved_age_seconds")
    ttl = integrity.get("approval_ttl_seconds")
    remaining = integrity.get("ttl_remaining_seconds")
    if age is not None or ttl is not None or remaining is not None:
        parts = []
        if age is not None:
            parts.append(f"approved_age_s={int(age)}")
        if ttl is not None:
            parts.append(f"ttl_s={int(ttl)}")
        if remaining is not None:
            parts.append(f"ttl_remaining_s={int(remaining)}")
        if parts:
            lines.append("approval_integrity_ttl: " + " ".join(parts))
    return lines


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
        # at the call site; enqueue_proposal already redacts when called from the
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


def cmd_code(args: argparse.Namespace) -> None:
    conn = get_connection()
    backend = getattr(args, "backend", "") or "zero"
    result = assistant.delegate_to_agent(
        conn,
        backend,
        args.objective,
        cwd=args.repo,
        timeout=args.timeout,
    )
    if result.get("error"):
        print(f"Coding delegation failed: {result['error']}")
        raise SystemExit(1)
    print(result.get("summary", "Coding task delegated."))
    for aid in result.get("proposed_action_ids", []):
        print(f"- proposed action #{aid} (review with `myos approve --list`)")
    if result.get("diff"):
        print("Patch remains approval-gated; apply only with `myos approve --action <id> --execute`.")


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
            for line in format_action_review_context(
                str(row["action_type"]),
                payload,
                requires_approval=bool(row["requires_approval"]),
            ):
                print(f"  {line}")
            _print_zero_approval_context(payload)
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


def _approval_queue_rows(conn, limit: int) -> list:
    return conn.execute(
        """
        SELECT id, agent_task_id, action_type, title, status, payload_json,
               requires_approval, created_at
        FROM agent_actions
        WHERE requires_approval=1 AND status IN ('proposed', 'approved')
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _approval_queue_json_entry(row) -> dict:
    """Machine-readable snapshot of one queued approval. Preview is bounded to
    a short redaction-safe snippet so downstream consumers never receive
    unbounded payload bodies."""
    payload = json.loads(row["payload_json"] or "{}")
    preview = payload.get("draft") or payload.get("text") or ""
    if not isinstance(preview, str):
        preview = str(preview)
    snippet = preview if len(preview) <= 220 else preview[:217] + "..."
    rollback = payload.get("rollback_note") or payload.get("rollback") or ""
    review_context = list(
        format_action_review_context(str(row["action_type"]), payload, requires_approval=True)
    )
    return {
        "id": int(row["id"]),
        "agent_task_id": int(row["agent_task_id"]) if row["agent_task_id"] is not None else None,
        "action_type": str(row["action_type"]),
        "title": str(row["title"] or ""),
        "status": str(row["status"] or ""),
        "created_at": str(row["created_at"] or ""),
        "target": _provider_target_summary(payload),
        "rollback": str(rollback) if rollback else "",
        "preview": snippet,
        "review_context": review_context,
    }


def cmd_approve(args: argparse.Namespace) -> None:
    conn = get_connection()
    json_mode = bool(getattr(args, "json", False))
    if args.list:
        rows = _approval_queue_rows(conn, int(args.limit))
        if json_mode:
            payload = {
                "schema": "myos.approve.list.v1",
                "count": len(rows),
                "limit": int(args.limit),
                "actions": [_approval_queue_json_entry(row) for row in rows],
            }
            print(json.dumps(payload, ensure_ascii=True))
            return
        if not rows:
            print("No approval-needed actions.")
            return
        print("Approval queue:")
        for row in rows:
            print(f"- action #{row['id']} task=#{row['agent_task_id']} [{row['action_type']}] {row['title']} status={row['status']}")
            payload = json.loads(row["payload_json"] or "{}")
            print(f"  target: {_provider_target_summary(payload)}")
            for line in format_action_review_context(str(row["action_type"]), payload, requires_approval=True):
                print(f"  {line}")
            _print_zero_approval_context(payload)
            rollback = payload.get("rollback_note") or payload.get("rollback")
            if rollback:
                print(f"  rollback: {rollback}")
            preview = payload.get("draft") or payload.get("text")
            if preview:
                snippet = str(preview) if len(str(preview)) <= 220 else str(preview)[:217] + "..."
                print(f"  preview: {snippet}")
        return
    if args.action is None:
        if json_mode:
            print(json.dumps({"schema": "myos.approve.list.v1", "error": "provide --action or --list"}, ensure_ascii=True))
        else:
            print("Provide --action ID or use --list.")
        raise SystemExit(1)
    cmd_act(argparse.Namespace(task=None, action=args.action, list=False, approve=True, execute=args.execute, limit=args.limit))


def _receipt_show_row(conn, receipt_id: int):
    return conn.execute(
        """
        SELECT r.*, a.title
        FROM action_execution_receipts r
        JOIN agent_actions a ON a.id = r.agent_action_id
        WHERE r.id = ?
        """,
        (int(receipt_id),),
    ).fetchone()


def _receipt_list_rows(conn, limit: int) -> list:
    return conn.execute(
        """
        SELECT id, agent_action_id, action_type, final_status, approved,
               follow_up_required, follow_up_inbox_id, request_json, created_at
        FROM action_execution_receipts
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


_OUTBOX_SENTINEL: object = object()


def _receipt_json_entry(row, *, outbox: object = _OUTBOX_SENTINEL) -> dict:
    """Machine-readable snapshot of one execution receipt. Includes the
    approval integrity envelope and verification receipt for automated
    audit consumers; excludes raw payload bodies so nothing user-derived
    leaks through the JSON channel. When `outbox` is passed explicitly
    (including `None`), it is always included so the show payload keeps a
    stable schema regardless of whether an outbox row exists."""
    try:
        request = json.loads(row["request_json"] or "{}")
    except (TypeError, ValueError):
        request = {}
    integrity = request.get("approval_integrity") if isinstance(request, dict) else None
    verification = request.get("verification") if isinstance(request, dict) else None
    approval_context = request.get("approval_context") if isinstance(request, dict) else None
    entry: dict = {
        "id": int(row["id"]),
        "agent_action_id": int(row["agent_action_id"]) if row["agent_action_id"] is not None else None,
        "action_type": str(row["action_type"] or ""),
        "final_status": str(row["final_status"] or ""),
        "approved": bool(row["approved"]),
        "follow_up_required": bool(row["follow_up_required"]),
        "follow_up_inbox_id": int(row["follow_up_inbox_id"]) if row["follow_up_inbox_id"] else None,
        "created_at": str(row["created_at"] or ""),
        "approval_integrity": integrity if isinstance(integrity, dict) else None,
        "verification": verification if isinstance(verification, dict) else None,
        "approval_context": approval_context if isinstance(approval_context, dict) else None,
    }
    if outbox is not _OUTBOX_SENTINEL:
        entry["outbox"] = outbox
    return entry


def cmd_execution_receipt(args: argparse.Namespace) -> None:
    conn = get_connection()
    json_mode = bool(getattr(args, "json", False))
    action = getattr(args, "receipt_action", "list") or "list"
    if action == "show":
        if not args.id:
            if json_mode:
                print(json.dumps({"schema": "myos.execution_receipt.show.v1", "error": "--id is required"}, ensure_ascii=True))
            else:
                print("--id is required for receipt show.")
            raise SystemExit(1)
        row = _receipt_show_row(conn, int(args.id))
        if not row:
            if json_mode:
                print(json.dumps({"schema": "myos.execution_receipt.show.v1", "error": "not_found", "id": int(args.id)}, ensure_ascii=True))
            else:
                print(f"Execution receipt #{args.id} not found.")
            raise SystemExit(1)
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
        outbox_dict = (
            {
                "id": int(outbox["id"]),
                "provider": str(outbox["provider"] or ""),
                "target_type": str(outbox["target_type"] or ""),
                "target_ref": str(outbox["target_ref"] or ""),
                "status": str(outbox["status"] or ""),
            }
            if outbox
            else None
        )
        if json_mode:
            entry = _receipt_json_entry(row, outbox=outbox_dict)
            entry["title"] = str(row["title"] or "")
            entry["result"] = str(row["result"] or "")
            entry["rollback_note"] = str(row["rollback_note"] or "")
            print(json.dumps({"schema": "myos.execution_receipt.show.v1", "receipt": entry}, ensure_ascii=True))
            return
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
            approval_context = request.get("approval_context") if isinstance(request, dict) else None
            context_lines = (
                format_compact_action_review_context(approval_context)
                if isinstance(approval_context, dict)
                else format_action_review_context(str(row["action_type"]), payload, requires_approval=bool(row["approved"]))
            )
            for line in context_lines:
                if ": " in line:
                    label, detail = line.split(": ", 1)
                    print(f"{label.replace('_', ' ').title()}: {detail}")
                else:
                    print(line)
        for line in _receipt_verification_lines(request):
            if ": " in line:
                label, detail = line.split(": ", 1)
                print(f"{label.replace('_', ' ').title()}: {detail}")
            else:
                print(line)
        for line in _receipt_integrity_lines(request):
            if ": " in line:
                label, detail = line.split(": ", 1)
                print(f"{label.replace('_', ' ').title()}: {detail}")
            else:
                print(line)
        if outbox_dict:
            print(
                f"Outbox: #{outbox_dict['id']} provider={outbox_dict['provider']} "
                f"target={outbox_dict['target_type']}:{outbox_dict['target_ref']} status={outbox_dict['status']}"
            )
        print(f"Result: {row['result']}")
        print(f"Rollback: {row['rollback_note']}")
        return

    rows = _receipt_list_rows(conn, int(args.limit))
    if json_mode:
        payload = {
            "schema": "myos.execution_receipt.list.v1",
            "count": len(rows),
            "limit": int(args.limit),
            "receipts": [_receipt_json_entry(row) for row in rows],
        }
        print(json.dumps(payload, ensure_ascii=True))
        return
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
        try:
            request = json.loads(row["request_json"] or "{}")
        except (TypeError, ValueError):
            request = {}
        approval_context = request.get("approval_context") if isinstance(request, dict) else None
        context_lines = (
            format_compact_action_review_context(approval_context)
            if isinstance(approval_context, dict)
            else format_action_review_context(str(row["action_type"]), {}, requires_approval=bool(row["approved"]))
        )
        for line in context_lines:
            print(f"  {line}")
        for line in _receipt_verification_lines(request):
            print(f"  {line}")
        for line in _receipt_integrity_lines(request):
            print(f"  {line}")


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


def cmd_action_provider(args: argparse.Namespace) -> None:
    """External-action provider entrypoint.

    Reads a single JSON request from stdin, applies redaction, drafts the
    corresponding outbox row, and — when `--execute` is passed with an
    approved action — dispatches the mutation through the connector
    execution path. All output is a single JSON object so downstream
    tooling (executors, tests, dashboards) can parse it directly.
    """
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
