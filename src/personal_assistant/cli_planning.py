from __future__ import annotations

import argparse
import json

from . import intents, plans
from .db import append_event, get_connection


def _intent_list_json_entry(row: dict) -> dict:
    """Structured, schema-stable projection of a single intent row for the
    `intent list --json` envelope. Kept intentionally narrow — just the fields
    a supervising process needs to route or prioritize — so richer show-mode
    fields don't leak here and cause consumers to conflate the two schemas."""
    return {
        "id": int(row["id"]),
        "status": str(row["status"] or ""),
        "priority": int(row["priority"]) if row["priority"] is not None else None,
        "objective": str(row["objective"] or ""),
        "success_criteria": str(row["success_criteria"] or ""),
    }


def _intent_show_json_payload(intent: dict) -> dict:
    """Full projection of an intent for `intent show --json`. This is a superset
    of `intent list --json` — the list schema is deliberately narrow for routing
    decisions; this show schema carries the evidence, decisions, and risks that
    a supervisor needs to reason about the intent's full state."""
    return {
        "schema": "myos.intent.show.v1",
        "intent": {
            "id": int(intent["id"]),
            "status": str(intent.get("status") or ""),
            "priority": int(intent["priority"]) if intent.get("priority") is not None else None,
            "objective": str(intent.get("objective") or ""),
            "context": str(intent.get("context") or ""),
            "success_criteria": str(intent.get("success_criteria") or ""),
            "constraints": [str(c) for c in (intent.get("constraints") or [])],
        },
        "evidence": [
            {
                "id": int(evidence["id"]),
                "source_type": str(evidence.get("source_type") or ""),
                "source_id": (str(evidence["source_id"]) if evidence.get("source_id") is not None else None),
                "summary": str(evidence.get("summary") or ""),
                "content": str(evidence.get("content") or ""),
                "confidence": float(evidence["confidence"]) if evidence.get("confidence") is not None else None,
                "created_at": str(evidence.get("created_at") or ""),
            }
            for evidence in (intent.get("evidence") or [])
        ],
        "decisions": [
            {
                "id": int(decision["id"]),
                "decision": str(decision.get("decision") or ""),
                "rationale": str(decision.get("rationale") or ""),
                "status": str(decision.get("status") or ""),
                "superseded_by": (
                    int(decision["superseded_by"]) if decision.get("superseded_by") is not None else None
                ),
                "created_at": str(decision.get("created_at") or ""),
            }
            for decision in (intent.get("decisions") or [])
        ],
        "risks": [
            {
                "id": int(risk["id"]),
                "risk": str(risk.get("risk") or ""),
                "impact": str(risk.get("impact") or ""),
                "likelihood": str(risk.get("likelihood") or ""),
                "mitigation": str(risk.get("mitigation") or ""),
                "owner": str(risk.get("owner") or ""),
                "due_date": str(risk.get("due_date") or ""),
                "status": str(risk.get("status") or ""),
                "created_at": str(risk.get("created_at") or ""),
            }
            for risk in (intent.get("risks") or [])
        ],
    }


def cmd_intent(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        action = getattr(args, "intent_action", "")
        json_mode = bool(getattr(args, "json", False))
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
            if json_mode:
                payload = {
                    "schema": "myos.intent.list.v1",
                    "count": len(rows),
                    "limit": int(args.limit),
                    "status_filter": str(args.status),
                    "intents": [_intent_list_json_entry(row) for row in rows],
                }
                print(json.dumps(payload, ensure_ascii=True))
                return
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
                if json_mode:
                    print(
                        json.dumps(
                            {"schema": "myos.intent.show.v1", "error": "not_found", "id": int(args.id)},
                            ensure_ascii=True,
                        )
                    )
                else:
                    print(f"Intent #{args.id} not found.")
                raise SystemExit(1)
            if json_mode:
                print(json.dumps(_intent_show_json_payload(intent), ensure_ascii=True))
                return
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
    finally:
        conn.close()


def _plan_show_json_payload(plan: dict) -> dict:
    """Full projection of a plan for `plan show --json`. Consumers use this to
    reason about steps/risks/validations without regex-parsing the human view."""
    return {
        "schema": "myos.plan.show.v1",
        "plan": {
            "id": int(plan["id"]),
            "intent_id": int(plan["intent_id"]) if plan.get("intent_id") is not None else None,
            "status": str(plan.get("status") or ""),
            "title": str(plan.get("title") or ""),
            "summary": str(plan.get("summary") or ""),
            "assumptions": [str(a) for a in (plan.get("assumptions") or [])],
        },
        "steps": [
            {
                "step_index": int(step["step_index"]) if step.get("step_index") is not None else None,
                "description": str(step.get("description") or ""),
                "status": str(step.get("status") or ""),
                "validation": str(step.get("validation") or ""),
            }
            for step in (plan.get("steps") or [])
        ],
        "risks": [
            {
                "severity": str(risk.get("severity") or ""),
                "risk": str(risk.get("risk") or ""),
                "mitigation": str(risk.get("mitigation") or ""),
            }
            for risk in (plan.get("risks") or [])
        ],
        "validations": [
            {
                "check_name": str(validation.get("check_name") or ""),
                "status": str(validation.get("status") or ""),
                "command": str(validation.get("command") or ""),
            }
            for validation in (plan.get("validations") or [])
        ],
    }


def cmd_plan(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        action = getattr(args, "plan_action", "")
        json_mode = bool(getattr(args, "json", False))
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

        if action == "list":
            rows = plans.list_plans(
                conn,
                intent_id=getattr(args, "intent", None),
                status=getattr(args, "status", "") or "",
                limit=int(getattr(args, "limit", 20) or 20),
            )
            if json_mode:
                payload = {
                    "schema": "myos.plan.list.v1",
                    "count": len(rows),
                    "limit": int(getattr(args, "limit", 20) or 20),
                    "intent_filter": (int(args.intent) if getattr(args, "intent", None) is not None else None),
                    "status_filter": str(getattr(args, "status", "") or ""),
                    "plans": [
                        {
                            "id": int(row["id"]),
                            "intent_id": (int(row["intent_id"]) if row.get("intent_id") is not None else None),
                            "title": str(row.get("title") or ""),
                            "summary": str(row.get("summary") or ""),
                            "status": str(row.get("status") or ""),
                            "created_at": str(row.get("created_at") or ""),
                            "updated_at": str(row.get("updated_at") or ""),
                        }
                        for row in rows
                    ],
                }
                print(json.dumps(payload, ensure_ascii=True))
                return
            if not rows:
                print("No plans found.")
                return
            print("Plans:")
            for row in rows:
                intent_ref = f"intent=#{row['intent_id']}" if row.get("intent_id") is not None else "intent=-"
                summary = f" summary={row['summary']}" if row.get("summary") else ""
                print(
                    f"- #{row['id']} {intent_ref} status={row['status']} "
                    f"updated={row['updated_at']} title={row['title'] or ''}{summary}"
                )
            return

        if action == "show":
            plan = plans.get_plan(conn, args.id)
            if plan is None:
                if json_mode:
                    print(
                        json.dumps(
                            {"schema": "myos.plan.show.v1", "error": "not_found", "id": int(args.id)},
                            ensure_ascii=True,
                        )
                    )
                else:
                    print(f"Plan #{args.id} not found.")
                raise SystemExit(1)
            if json_mode:
                print(json.dumps(_plan_show_json_payload(plan), ensure_ascii=True))
                return
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
    finally:
        conn.close()


def cmd_evidence(args: argparse.Namespace) -> None:
    conn = get_connection()
    action = getattr(args, "evidence_action", "")
    json_mode = bool(getattr(args, "json", False))
    if action == "attach":
        try:
            evidence_id = plans.attach_retrieval_run_evidence(
                conn,
                intent_id=args.intent,
                retrieval_run_id=args.retrieval_run,
            )
        except ValueError as exc:
            if json_mode:
                print(
                    json.dumps(
                        {
                            "schema": "myos.evidence.attach.v1",
                            "error": "invalid_request",
                            "message": str(exc),
                            "intent_id": int(args.intent),
                            "retrieval_run_id": int(args.retrieval_run),
                        },
                        ensure_ascii=True,
                    )
                )
            else:
                print(str(exc))
            raise SystemExit(1) from exc
        conn.commit()
        if json_mode:
            print(
                json.dumps(
                    {
                        "schema": "myos.evidence.attach.v1",
                        "evidence_id": int(evidence_id),
                        "intent_id": int(args.intent),
                        "retrieval_run_id": int(args.retrieval_run),
                    },
                    ensure_ascii=True,
                )
            )
            return
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
        params: tuple[object, ...] = (
            (int(args.limit),) if args.connector == "all" else (args.connector, int(args.limit))
        )
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


def _review_packet_json_payload(packet_id: int, plan_id: int, packet: dict | None) -> dict:
    """Full projection of a review packet for `review-packet --json`. Supervisors
    consume this instead of regex-parsing the human view so they can gate on the
    exact evidence, risks, and rollback commitments the packet enshrines."""
    if not packet:
        return {
            "schema": "myos.review_packet.v1",
            "packet_id": int(packet_id),
            "plan_id": int(plan_id),
            "summary": "",
            "packet": {},
        }
    body = packet.get("packet") or {}
    return {
        "schema": "myos.review_packet.v1",
        "packet_id": int(packet_id),
        "plan_id": int(plan_id),
        "summary": str(packet.get("summary") or ""),
        "packet": body,
    }


def cmd_review_packet(args: argparse.Namespace) -> None:
    conn = get_connection()
    json_mode = bool(getattr(args, "json", False))
    try:
        packet_id = plans.create_review_packet(
            conn,
            plan_id=args.plan,
            retrieval_run_id=args.retrieval_run,
        )
    except ValueError as exc:
        if json_mode:
            print(
                json.dumps(
                    {
                        "schema": "myos.review_packet.v1",
                        "error": "invalid_request",
                        "message": str(exc),
                        "plan_id": int(args.plan),
                    },
                    ensure_ascii=True,
                )
            )
        else:
            print(str(exc))
        raise SystemExit(1) from exc
    conn.commit()
    packet = plans.get_review_packet(conn, packet_id)
    if json_mode:
        print(json.dumps(_review_packet_json_payload(packet_id, args.plan, packet), ensure_ascii=True))
        return
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
