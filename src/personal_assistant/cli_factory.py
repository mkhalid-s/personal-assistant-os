from __future__ import annotations

import argparse
import json

from . import autonomy, cli_autonomy, factory
from .db import get_connection


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
        cli_autonomy.print_recommendations(
            conn,
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
