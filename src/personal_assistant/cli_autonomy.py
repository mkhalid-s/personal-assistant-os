from __future__ import annotations

import argparse
import sqlite3

from . import autonomy, autonomy_loop, command_registry
from .db import get_connection


def command_autonomy_decision(conn: sqlite3.Connection, command: str, *, requested_mode: str = "") -> dict[str, object]:
    spec = command_registry.find_command(command)
    return autonomy.decide_command(
        command,
        safety=spec.safety if spec else "unknown",
        requires_confirmation=bool(spec.requires_confirmation) if spec else True,
        level=autonomy.level_from_policy(conn),
        requested_mode=requested_mode,
    )


def print_autonomy_decision(decision: dict[str, object]) -> None:
    print(
        "Autonomy: "
        f"decision={decision['decision']} tier={decision['tier']} "
        f"safety={decision['safety']} reason={decision['reason']}"
    )


def print_recommendations(conn: sqlite3.Connection, recommendations: list[dict[str, object]]) -> None:
    for item in autonomy.ranked_recommendations(conn, recommendations)[:2]:
        command = str(item.get("command") or "").strip()
        reason = str(item.get("reason") or "").strip()
        label = str(item.get("label") or "").strip()
        if command:
            reason = reason.rstrip(".")
        suffix = f" -> {command}" if command else ""
        feedback = f" [label={label}]" if label else ""
        print(f"Recommendation: {reason}{suffix}{feedback}")


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
    if action == "recommendation-feedback":
        useful = args.useful == "yes"
        try:
            feedback_id = autonomy.record_recommendation_feedback(
                conn,
                label=args.label,
                command=args.recommendation_command,
                decision=args.decision,
                intent=args.intent,
                workflow_pack=args.workflow_pack,
                useful=useful,
                note=args.note or "",
            )
        except ValueError as exc:
            print(f"Recommendation feedback failed: {exc}")
            raise SystemExit(1) from exc
        print(f"Recommendation feedback recorded: #{feedback_id} useful={useful}")
        print("Privacy: note text was hashed; raw recommendation feedback text was not stored.")
        return
    if action == "recommendations":
        rows = autonomy.recommendation_feedback_summary(conn, limit=args.limit)
        if not rows:
            print("No recommendation feedback recorded.")
            return
        print("Recommendation feedback summary:")
        for row in rows:
            command = f" command={row['command']}" if row["command"] else ""
            side_effects = ",".join(row.get("side_effects") or []) or "none"
            mixed_recent = " mixed_recent=yes" if row.get("mixed_recent_feedback") else ""
            print(
                f"- key={row['recommendation_key'][:12]} surface={row.get('surface', 'general')} "
                f"label={row['label']}{command} "
                f"score={row['score']} useful={row['useful_count']} not_useful={row['not_useful_count']} "
                f"recent_score_{row.get('recent_score_window_days', 30)}d={row.get('recent_score', 0)} "
                f"learning_score={row.get('learning_score', 0)} side_effects={side_effects}{mixed_recent} "
                f"last={row['last_feedback_at']}"
            )
        return
    raise SystemExit("Unknown autonomy command.")


def print_loop_result(result: dict[str, object]) -> None:
    print(f"Autonomy loop task #{result['task_id']} run #{result['run_id']} status={result['status']}")
    print(
        f"- provider={result['provider']} proposed={len(result['action_ids'])} "
        f"safe_executed={result['executed_now']} pending_approvals={result['pending_approvals']} "
        f"blocked_or_failed={result['blocked_or_failed']}"
    )
    if result.get("summary"):
        print(f"- summary: {result['summary']}")
    if result.get("pending_approvals"):
        print("Recommendation: review pending approvals -> myos approve --list [label=review_approvals]")
    else:
        print(f"Recommendation: inspect loop status -> myos loop status --task {result['task_id']} [label=inspect_loop_status]")


def print_goal_cycle_result(result: dict[str, object]) -> None:
    action = result.get("action")
    goal = result.get("goal_id")
    goal_label = f" goal=#{goal}" if goal is not None else ""
    print(f"Goal scheduler: action={action}{goal_label}")
    if action in {"started", "resumed"}:
        print_loop_result(result)
        return
    if result.get("summary"):
        print(f"- summary: {result['summary']}")
    if action == "skipped" and result.get("pending_approvals"):
        print("Recommendation: review pending approvals -> myos approve --list [label=review_approvals]")
    elif action == "noop":
        print("Recommendation: review assistant goals -> myos goal list [label=review_goals]")


def cmd_loop(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "loop_action", "")
    try:
        if action == "start":
            result = autonomy_loop.start_loop(
                conn,
                args.objective,
                context=args.context,
                backend=args.backend,
                max_actions=args.max_actions,
                mode=args.mode,
            )
            print_loop_result(result)
            return
        if action == "resume":
            result = autonomy_loop.resume_loop(conn, args.task, max_actions=args.max_actions)
            print_loop_result(result)
            return
        if action == "status":
            rows = autonomy_loop.loop_status(conn, task_id=args.task, limit=args.limit)
            if not rows:
                print("No autonomy loop tasks found.")
                return
            print("Autonomy loop tasks:")
            for row in rows:
                objective = row["objective"] if len(row["objective"]) <= 90 else row["objective"][:87] + "..."
                print(
                    f"- task #{row['task_id']} status={row['status']} cycles={row['cycles']} "
                    f"provider={row['provider'] or row['backend'] or 'local'} "
                    f"actions={row['total_actions']} executed={row['executed']} "
                    f"pending_approvals={row['pending_approvals']} objective={objective}"
                )
                if row["pending_approvals"]:
                    print("  Recommendation: myos approve --list [label=review_approvals]")
            return
        if action == "goals":
            goals = autonomy_loop.eligible_goals(conn, limit=args.limit)
            if not goals:
                print("No eligible assistant goals are due.")
                print("Recommendation: review assistant goals -> myos goal list [label=review_goals]")
                return
            print("Eligible autonomy goals:")
            for goal in goals:
                loop = goal.get("loop") if isinstance(goal.get("loop"), dict) else {}
                loop_summary = (
                    f" loop_task=#{loop['task_id']} status={loop['status']} pending_approvals={loop['pending_approvals']}"
                    if loop
                    else " loop_task=none"
                )
                objective = goal["objective"] if len(goal["objective"]) <= 90 else goal["objective"][:87] + "..."
                print(
                    f"- goal #{goal['goal_id']} priority={goal['priority']} "
                    f"cadence_min={goal['cadence_minutes']}{loop_summary} objective={objective}"
                )
                if int(loop.get("pending_approvals") or 0) > 0:
                    print("  Recommendation: myos approve --list [label=review_approvals]")
                else:
                    print(f"  Recommendation: myos loop run-goal --goal {goal['goal_id']} [label=run_goal_cycle]")
            return
        if action == "run-goal":
            result = autonomy_loop.run_goal_cycle(
                conn,
                goal_id=args.goal,
                backend=args.backend,
                max_actions=args.max_actions,
                limit=args.limit,
            )
            print_goal_cycle_result(result)
            return
        if action == "ledger":
            rows = autonomy_loop.list_ledger(
                conn,
                limit=args.limit,
                goal_id=args.goal,
                task_id=args.task,
                status=args.status,
            )
            if not rows:
                print("No autonomy ledger entries found.")
                filters = []
                if args.goal is not None:
                    filters.append(f"goal=#{args.goal}")
                if args.task is not None:
                    filters.append(f"task=#{args.task}")
                if args.status:
                    filters.append(f"status={args.status}")
                if filters:
                    print(f"- filters: {', '.join(filters)}")
                return
            print("Autonomy run ledger:")
            for row in rows:
                goal = f" goal=#{row['assistant_goal_id']}" if row["assistant_goal_id"] is not None else ""
                task = f" task=#{row['agent_task_id']}" if row["agent_task_id"] is not None else ""
                run = f" run=#{row['agent_run_id']}" if row["agent_run_id"] is not None else ""
                provider = f" provider={row['provider']}" if row["provider"] else ""
                reason = str(row["reason"] or "")
                if len(reason) > 120:
                    reason = reason[:117] + "..."
                print(
                    f"- ledger #{row['id']} decision={row['decision_type']} status={row['status']}"
                    f"{goal}{task}{run}{provider} proposed={row['actions_proposed']}"
                    f" safe_executed={row['safe_actions_executed']} pending={row['pending_approvals']}"
                    f" blocked={row['blocked_or_failed']} created={row['created_at']}"
                )
                if reason:
                    print(f"  reason: {reason}")
                if int(row["pending_approvals"] or 0) > 0:
                    print("  Recommendation: myos approve --list [label=review_approvals]")
            return
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from exc
    raise SystemExit("Unknown loop command.")
