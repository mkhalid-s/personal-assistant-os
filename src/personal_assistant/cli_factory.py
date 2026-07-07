from __future__ import annotations

import argparse
import json
import os

from . import autonomy, cli_autonomy, factory, plans
from .approval_context import format_factory_review_context
from .db import get_connection


def _executor_artifacts(conn, run: dict) -> list[dict]:
    packet = next((a for a in run["artifacts"] if a["artifact_type"] == "review_packet"), None)
    if not packet:
        return []
    review_packet = plans.get_review_packet(conn, int(packet["artifact_id"]))
    if not review_packet:
        return []
    artifacts = review_packet.get("packet", {}).get("executor_artifacts") or []
    return [artifact for artifact in artifacts if isinstance(artifact, dict)]


def _print_executor_artifacts(conn, run: dict) -> None:
    artifacts = _executor_artifacts(conn, run)
    if not artifacts:
        return
    print("Executor artifacts:")
    for artifact in artifacts:
        if artifact.get("type") != "zero_executor":
            print(f"- {artifact.get('type', 'executor')}: status={artifact.get('status', 'unknown')}")
            continue
        action_id = artifact.get("agent_action_id")
        action = f" action=#{action_id}" if action_id else ""
        exit_code = artifact.get("exit_code")
        exit_part = f" exit_code={exit_code}" if exit_code is not None else ""
        print(f"- zero status={artifact.get('status', 'unknown')}{exit_part}{action}")
        run_id = str(artifact.get("run_id") or "").strip()
        session_id = str(artifact.get("session_id") or "").strip()
        if run_id or session_id:
            print(f"  zero_ref=run:{run_id or '-'} session:{session_id or '-'}")
        if bool(artifact.get("executor_isolated_worktree")):
            retained = bool(artifact.get("executor_worktree_retained"))
            print(f"  executor_worktree=isolated retained={retained}")
        permission_count = int(artifact.get("permission_events_count") or 0)
        warning_count = len(artifact.get("warnings") or [])
        error_count = len(artifact.get("errors") or [])
        protocol_count = len(artifact.get("protocol_errors") or [])
        if permission_count or warning_count or error_count or protocol_count:
            print(
                "  signals="
                f"permissions:{permission_count} warnings:{warning_count} "
                f"errors:{error_count} protocol_errors:{protocol_count}"
            )
        for warning in [
            str(item).strip().replace("\n", " ") for item in artifact.get("warnings") or [] if str(item).strip()
        ][:2]:
            print(f"  warning={warning[:200]}")
        for error in artifact.get("errors") or []:
            if not isinstance(error, dict):
                continue
            code = str(error.get("code") or "error")
            message = str(error.get("message") or "").strip().replace("\n", " ")
            print(f"  error={code}: {message[:200]}")
        changed_files = [str(item) for item in artifact.get("changed_files") or [] if str(item)]
        if changed_files:
            print(f"  changed_files={','.join(changed_files)}")
        diff_stats = artifact.get("diff_stats") if isinstance(artifact.get("diff_stats"), dict) else {}
        if diff_stats:
            print(
                "  diff_stats="
                f"files:{int(diff_stats.get('files') or 0)} "
                f"+{int(diff_stats.get('additions') or 0)} "
                f"-{int(diff_stats.get('deletions') or 0)} "
                f"binary:{int(diff_stats.get('binary_files') or 0)}"
            )
        if artifact.get("diff_too_large"):
            print(
                "  diff_notice="
                f"oversized_patch bytes:{int(artifact.get('diff_bytes') or 0)} "
                f"limit:{int(artifact.get('diff_limit_bytes') or 0)}"
            )
        verification_commands = [
            str(item).strip() for item in artifact.get("verification_commands") or [] if str(item).strip()
        ]
        for command in verification_commands[:5]:
            print(f"  verify={command}")
        summary = str(artifact.get("summary") or "").strip().replace("\n", " ")
        if summary:
            print(f"  summary={summary[:240]}")
        approval_command = str(artifact.get("approval_command") or "").strip()
        if approval_command:
            print(f"  approve={approval_command}")
        retry_command = str(artifact.get("retry_command") or "").strip()
        if retry_command:
            print(f"  retry={retry_command}")
        follow_up_id = artifact.get("follow_up_inbox_id")
        if follow_up_id:
            print(f"  follow_up=inbox_item#{follow_up_id}")


def cmd_factory(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "factory_action", "")

    if action == "start":
        autonomy_decision = cli_autonomy.command_autonomy_decision(conn, "factory", requested_mode=args.mode)
        cli_autonomy.print_autonomy_decision(autonomy_decision)
        cli_autonomy.print_recommendations(
            conn,
            autonomy.recommend_next_steps(
                autonomy_decision,
                command="factory",
                workflow_pack=args.pack,
            ),
        )
        if autonomy_decision["decision"] == autonomy.BLOCKED:
            raise SystemExit(1)
        try:
            result = factory.start_review_first_run(
                conn,
                intent_id=args.intent,
                mode=args.mode,
                workflow_pack=args.pack,
                executor_backend=getattr(args, "executor", "local"),
                executor_context={
                    "repo": os.path.abspath(getattr(args, "repo", ".")),
                    "timeout": getattr(args, "timeout", 600),
                    "max_turns": getattr(args, "max_turns", 0),
                    "verification_commands": getattr(args, "verify_command", []) or [],
                },
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Factory run #{result['id']} for intent #{result['intent_id']} status={result['status']}")
        executor = result.get("executor_backend", "local")
        executor_part = f" executor={executor}" if executor != "local" else ""
        print(f"mode={args.mode} pack={args.pack}{executor_part} plan=#{result['plan_id']}")
        if result["retrieval_run_id"] is not None:
            print(f"retrieval_run=#{result['retrieval_run_id']}")
        print(f"review_packet=#{result['review_packet_id']}")
        print("agent_runs=" + ",".join(f"#{run_id}" for run_id in result["agent_run_ids"]))
        action_ids = result.get("proposed_action_ids") or []
        if action_ids:
            print("approval_actions=" + ",".join(f"#{action_id}" for action_id in action_ids))
            if len(action_ids) == 1:
                print(f"approve=myos approve --action {action_ids[0]} --execute")
        print(f"stopped_before_execution={args.mode == 'review_first'}")
        cli_autonomy.print_recommendations(
            conn,
            autonomy.recommend_next_steps(
                autonomy_decision,
                command="factory",
                workflow_pack=args.pack,
                factory_run_id=result["id"],
            ),
        )
        return

    if action == "status":
        run = factory.get_factory_run(conn, args.id)
        json_mode = bool(getattr(args, "json", False))
        if run is None:
            if json_mode:
                print(
                    json.dumps(
                        {"schema": "myos.factory.status.v1", "error": "not_found", "id": int(args.id)},
                        ensure_ascii=True,
                    )
                )
            else:
                print(f"Factory run #{args.id} not found.")
            raise SystemExit(1)
        if json_mode:
            payload = {
                "schema": "myos.factory.status.v1",
                "run": {
                    "id": int(run["id"]),
                    "intent_id": int(run["intent_id"]) if run.get("intent_id") is not None else None,
                    "plan_id": int(run["plan_id"]) if run.get("plan_id") is not None else None,
                    "mode": str(run.get("mode") or ""),
                    "workflow_pack": str(run.get("workflow_pack") or ""),
                    "executor_backend": str(run.get("executor_backend") or "local"),
                    "status": str(run.get("status") or ""),
                    "summary": str(run.get("summary") or ""),
                    "outcome": str(run.get("outcome") or ""),
                    "outcome_notes": str(run.get("outcome_notes") or ""),
                },
                "stages": [
                    {
                        "stage_name": str(stage.get("stage_name") or ""),
                        "status": str(stage.get("status") or ""),
                        "role": str(stage.get("role") or ""),
                        "agent_run_id": int(stage["agent_run_id"]) if stage.get("agent_run_id") else None,
                    }
                    for stage in run["stages"]
                ],
                "artifacts": [
                    {
                        "artifact_type": str(artifact.get("artifact_type") or ""),
                        "artifact_id": int(artifact["artifact_id"])
                        if artifact.get("artifact_id") is not None
                        else None,
                        "label": str(artifact.get("label") or ""),
                    }
                    for artifact in run["artifacts"]
                ],
                "executor_artifacts": _executor_artifacts(conn, run),
            }
            print(json.dumps(payload, ensure_ascii=True))
            return
        executor = run.get("executor_backend", "local")
        executor_part = f" executor={executor}" if executor != "local" else ""
        print(
            f"Factory run #{run['id']} intent=#{run['intent_id']} plan=#{run['plan_id']} "
            f"mode={run['mode']} pack={run['workflow_pack']}{executor_part} status={run['status']}"
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
        _print_executor_artifacts(conn, run)
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
        json_mode = bool(getattr(args, "json", False))
        if run is None:
            if json_mode:
                print(
                    json.dumps(
                        {"schema": "myos.factory.review.v1", "error": "not_found", "id": int(args.id)},
                        ensure_ascii=True,
                    )
                )
            else:
                print(f"Factory run #{args.id} not found.")
            raise SystemExit(1)
        post_review = {"execution", "learning"}
        gaps = [
            s["stage_name"]
            for s in run["stages"]
            if s["status"] in {"pending", "blocked"} and s["stage_name"] not in post_review
        ]
        packet = next((a for a in run["artifacts"] if a["artifact_type"] == "review_packet"), None)
        readiness = "ready_for_approval" if not gaps and packet else "needs_attention"
        if json_mode:
            payload = {
                "schema": "myos.factory.review.v1",
                "id": int(args.id),
                "readiness": readiness,
                "review_packet_id": int(packet["artifact_id"]) if packet else None,
                "gaps": gaps,
                "workflow_pack": str(run.get("workflow_pack") or ""),
                "review_context": list(format_factory_review_context(str(run.get("workflow_pack") or ""))),
                "executor_artifacts": _executor_artifacts(conn, run),
                "execution_gate": "approval_required",
            }
            print(json.dumps(payload, ensure_ascii=True))
            return
        print(f"Factory review #{args.id}: {readiness}")
        print(f"review_packet=#{packet['artifact_id'] if packet else 'missing'}")
        if gaps:
            print("Open gaps: " + ", ".join(gaps))
        for line in format_factory_review_context(str(run.get("workflow_pack") or "")):
            print(line)
        _print_executor_artifacts(conn, run)
        print("Execution remains approval-gated.")
        return

    if action == "list":
        json_mode = bool(getattr(args, "json", False))
        status_filter = getattr(args, "status", "") or ""
        limit = int(getattr(args, "limit", 20) or 20)
        if status_filter and status_filter != "all":
            rows = conn.execute(
                """
                SELECT id, intent_id, plan_id, mode, workflow_pack, executor_backend, status,
                       summary, outcome, started_at, finished_at
                FROM factory_runs
                WHERE status = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, intent_id, plan_id, mode, workflow_pack, executor_backend, status,
                       summary, outcome, started_at, finished_at
                FROM factory_runs
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        if json_mode:
            payload = {
                "schema": "myos.factory.list.v1",
                "count": len(rows),
                "limit": limit,
                "status_filter": str(status_filter) if status_filter else "all",
                "runs": [
                    {
                        "id": int(row["id"]),
                        "intent_id": int(row["intent_id"]) if row["intent_id"] is not None else None,
                        "plan_id": int(row["plan_id"]) if row["plan_id"] is not None else None,
                        "mode": str(row["mode"] or ""),
                        "workflow_pack": str(row["workflow_pack"] or ""),
                        "executor_backend": str(row["executor_backend"] or "local"),
                        "status": str(row["status"] or ""),
                        "summary": str(row["summary"] or ""),
                        "outcome": str(row["outcome"] or ""),
                        "started_at": str(row["started_at"] or ""),
                        "finished_at": str(row["finished_at"] or ""),
                    }
                    for row in rows
                ],
            }
            print(json.dumps(payload, ensure_ascii=True))
            return
        if not rows:
            print("No factory runs recorded.")
            return
        print("Factory runs:")
        for row in rows:
            executor = row["executor_backend"] or "local"
            executor_part = f" executor={executor}" if executor != "local" else ""
            outcome = f" outcome={row['outcome']}" if row["outcome"] else ""
            print(
                f"- run #{row['id']} intent=#{row['intent_id']} plan=#{row['plan_id']} "
                f"mode={row['mode']} pack={row['workflow_pack']}{executor_part} "
                f"status={row['status']} started={row['started_at']}{outcome}"
            )
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
        factory.record_stage(
            conn, factory_run_id=args.id, stage_name="approval", status="completed", note="factory approval recorded"
        )
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
            print(
                f"Factory policy #{policy_id} {scope} connector={args.connector or 'all'} action={args.action_type or 'all'} mode={args.mode}"
            )
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
            print(
                f"- #{row['id']} {scope} connector={connector} action={action_type} mode={row['allowed_mode']} status={row['status']}"
            )
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
        print(f"side_effects={json.dumps(insights['side_effects'], ensure_ascii=True, sort_keys=True)}")
        print(f"useful_sources={json.dumps(insights['useful_sources'], ensure_ascii=True, sort_keys=True)}")
        if insights["notes"]:
            print("Notes:")
            for note in insights["notes"]:
                print(f"- {note}")
        return

    raise SystemExit("Unknown factory command.")
