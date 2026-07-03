from __future__ import annotations

import argparse
import json

from . import intents, plans
from .db import append_event, get_connection


def cmd_intent(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "intent_action", "")
    if action == "create":
        intent_id = intents.create_intent(
            conn,
            objective=args.objective,
            context=args.context,
            constraints=args.constraint,
            success_criteria=args.success,
            priority=args.priority,
        )
        conn.commit()
        intent = intents.get_intent(conn, intent_id)
        objective = intent["objective"] if intent else args.objective
        print(f"Created intent #{intent_id}: {objective}")
        return

    if action == "list":
        rows = intents.list_intents(conn, status=args.status, limit=args.limit)
        if not rows:
            print("No intents found.")
            return
        print("Intents:")
        for row in rows:
            success = f" success={row['success_criteria']}" if row["success_criteria"] else ""
            print(
                f"- #{row['id']} status={row['status']} priority={row['priority']}"
                f" objective={row['objective']}{success}"
            )
        return

    if action == "show":
        intent = intents.get_intent(conn, args.id)
        if intent is None:
            print(f"Intent #{args.id} not found.")
            raise SystemExit(1)
        print(f"Intent #{intent['id']}")
        print(f"Status: {intent['status']}")
        print(f"Priority: {intent['priority']}")
        print(f"Objective: {intent['objective']}")
        if intent.get("context"):
            print(f"Context: {intent['context']}")
        if intent.get("success_criteria"):
            print(f"Success: {intent['success_criteria']}")
        if intent.get("constraints"):
            print("Constraints:")
            for constraint in intent["constraints"]:
                print(f"- {constraint}")
        print("Evidence:")
        for evidence in intent["evidence"]:
            source_id = f":{evidence['source_id']}" if evidence["source_id"] is not None else ""
            summary = f" summary={evidence['summary']}" if evidence["summary"] else ""
            print(
                f"- #{evidence['id']} source={evidence['source_type']}{source_id}"
                f" confidence={evidence['confidence']:.2f}{summary}"
            )
            print(f"  {evidence['content']}")
        if not intent["evidence"]:
            print("- none")
        return

    if action == "evidence" and getattr(args, "evidence_action", "") == "add":
        try:
            evidence_id = intents.add_evidence(
                conn,
                intent_id=args.id,
                content=args.text,
                source_type=args.source_type,
                source_id=args.source_id,
                summary=args.summary,
                confidence=args.confidence,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Added evidence #{evidence_id} to intent #{args.id}.")
        return

    raise SystemExit("Unknown intent command.")


def cmd_plan(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "plan_action", "")
    if action == "create":
        try:
            plan_id = plans.create_plan(
                conn,
                intent_id=args.intent,
                title=args.title,
                assumptions=args.assumption,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        plan = plans.get_plan(conn, plan_id)
        print(f"Created plan #{plan_id} for intent #{args.intent}: {plan['title'] if plan else args.title}")
        return

    if action == "show":
        plan = plans.get_plan(conn, args.id)
        if plan is None:
            print(f"Plan #{args.id} not found.")
            raise SystemExit(1)
        print(f"Plan #{plan['id']} intent={plan['intent_id']} status={plan['status']}")
        print(f"Title: {plan['title']}")
        if plan.get("summary"):
            print(f"Summary: {plan['summary']}")
        if plan.get("assumptions"):
            print("Assumptions:")
            for assumption in plan["assumptions"]:
                print(f"- {assumption}")
        print("Steps:")
        for step in plan["steps"]:
            print(f"{step['step_index']}. {step['description']} [{step['status']}]")
            if step["validation"]:
                print(f"   validation: {step['validation']}")
        print("Risks:")
        for risk in plan["risks"]:
            print(f"- [{risk['severity']}] {risk['risk']} -> {risk['mitigation']}")
        print("Validations:")
        for validation in plan["validations"]:
            command = f" command={validation['command']}" if validation["command"] else ""
            print(f"- {validation['check_name']} [{validation['status']}]{command}")
        return

    raise SystemExit("Unknown plan command.")


def cmd_evidence(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "evidence_action", "")
    if action == "attach":
        try:
            evidence_id = plans.attach_retrieval_run_evidence(
                conn,
                intent_id=args.intent,
                retrieval_run_id=args.retrieval_run,
            )
        except ValueError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        print(f"Attached retrieval run #{args.retrieval_run} as evidence #{evidence_id} to intent #{args.intent}.")
        return
    if action == "sync-external":
        intent = intents.get_intent(conn, args.intent)
        if intent is None:
            print(f"Intent #{args.intent} not found.")
            raise SystemExit(1)
        objective_terms = {
            term.strip(".,:;!?()[]{}").lower()
            for term in str(intent["objective"]).split()
            if len(term.strip(".,:;!?()[]{}")) >= 4
        }
        connector_clause = "" if args.connector == "all" else "WHERE connector = ?"
        params: tuple[object, ...] = (int(args.limit),) if args.connector == "all" else (args.connector, int(args.limit))
        rows = conn.execute(
            f"""
            SELECT id, connector, external_id, item_type, title, body, url
            FROM external_items
            {connector_clause}
            ORDER BY fetched_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        attached = 0
        for row in rows:
            haystack = f"{row['title']} {row['body'] or ''}".lower()
            if objective_terms and not any(term in haystack for term in objective_terms):
                continue
            source_id = str(row["id"])
            exists = conn.execute(
                """
                SELECT 1 FROM intent_evidence
                WHERE intent_id = ? AND source_type = 'external_item' AND source_id = ?
                LIMIT 1
                """,
                (int(args.intent), source_id),
            ).fetchone()
            if exists:
                continue
            content_parts = [
                f"{row['connector']}:{row['external_id']} ({row['item_type']})",
                row["title"],
            ]
            if row["body"]:
                content_parts.append(str(row["body"])[:1000])
            if row["url"]:
                content_parts.append(str(row["url"]))
            intents.add_evidence(
                conn,
                intent_id=args.intent,
                content="\n".join(content_parts),
                source_type="external_item",
                source_id=source_id,
                summary=f"{row['connector']} {row['item_type']}: {row['title']}",
                confidence=0.75,
            )
            attached += 1
        append_event(
            conn,
            "connector_evidence_synced",
            "intent",
            int(args.intent),
            json.dumps({"connector": args.connector, "attached": attached}, ensure_ascii=True),
        )
        conn.commit()
        print(f"Attached {attached} external evidence item(s) to intent #{args.intent}.")
        return
    raise SystemExit("Unknown evidence command.")


def cmd_review_packet(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        packet_id = plans.create_review_packet(
            conn,
            plan_id=args.plan,
            retrieval_run_id=args.retrieval_run,
        )
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from exc
    conn.commit()
    packet = plans.get_review_packet(conn, packet_id)
    print(f"Review packet #{packet_id} for plan #{args.plan}")
    if packet:
        body = packet["packet"]
        print(f"Summary: {packet['summary']}")
        print(f"Intent: #{body['intent']['id']} {body['intent']['objective']}")
        print("Steps:")
        for step in body["plan"]["steps"]:
            print(f"- {step['description']}")
        print("Risks:")
        for risk in body["plan"]["risks"]:
            print(f"- {risk['risk']} -> {risk['mitigation']}")
        print("Evidence:")
        if body["evidence"]:
            for evidence in body["evidence"]:
                source_id = f":{evidence['source_id']}" if evidence["source_id"] is not None else ""
                print(f"- {evidence['source_type']}{source_id}: {evidence['summary'] or evidence['content'][:80]}")
        else:
            print("- none")
        if body["retrieval_sources"]:
            print("Retrieval sources:")
            for source in body["retrieval_sources"]:
                print(f"- {source['citation']} score={source['score']:.3f} reason={source['reason']}")
        print(f"Approval required: {body['approval_required']}")
        print(f"Rollback: {body['rollback_note']}")
