from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import agentcore, autonomy, command_registry, factory, graphrag, intents, observability, plans
from .db import append_event, verify_schema


COMMAND_TIERS: dict[str, list[str]] = command_registry.command_inventory()
DEFAULT_EVAL_FIXTURE_PATH = Path(__file__).with_name("route_eval_fixtures.json")
ROUTABLE_INTENTS = (
    "capture",
    "retrieve_context",
    "daily_brief",
    "plan_intent",
    "factory_run",
    "connector_update",
    "approval_review",
    "system_health",
    "unknown",
)


@dataclass
class RouteDecision:
    intent: str
    confidence: float
    reason: str
    recommended_workflow: str
    requires_confirmation: bool = False
    proposed_actions: list[dict[str, Any]] = field(default_factory=list)
    command_tier: str = "daily"
    workflow_pack: str = ""
    backend: str = "heuristic"
    fallback_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def command_inventory() -> dict[str, list[str]]:
    return command_registry.command_inventory()


def command_catalog(*, limit: int = 40) -> list[dict[str, Any]]:
    return command_registry.compact_catalog(limit=limit)


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _heuristic_route_text(text: str, *, surface: str = "cli") -> RouteDecision:
    raw = (text or "").strip()
    lowered = raw.lower()
    if not raw:
        return RouteDecision(
            intent="unknown",
            confidence=0.0,
            reason="empty request",
            recommended_workflow="Ask the user what they want MYOS to do.",
            requires_confirmation=True,
        )
    if _contains(
        lowered, "approve", "approval", "pending action", "receipt", "outbox", "what happened with that action"
    ):
        return RouteDecision(
            intent="approval_review",
            confidence=0.9,
            reason="request mentions approvals, receipts, or outbox state",
            recommended_workflow="Review pending approvals and recent execution receipts.",
        )
    if _contains(lowered, "healthy", "health", "doctor", "release check", "release-check", "sanity", "is the system"):
        return RouteDecision(
            intent="system_health",
            confidence=0.88,
            reason="request asks about system readiness or diagnostics",
            recommended_workflow="Run local schema and readiness checks.",
            command_tier="diagnostic",
        )
    if _contains(lowered, "jira", "github", "confluence", "aha") and _contains(
        lowered, "draft", "update", "comment", "send", "post"
    ):
        return RouteDecision(
            intent="connector_update",
            confidence=0.86,
            reason="request asks for connector-specific updates",
            recommended_workflow="Create approval-gated connector dry-run drafts.",
            requires_confirmation=True,
            command_tier="workflow",
            workflow_pack="connector_ops",
        )
    if _contains(lowered, "autonomous", "autonomously", "factory", "work on this", "execute this", "run this"):
        return RouteDecision(
            intent="factory_run",
            confidence=0.84,
            reason="request asks MYOS to work through an intent or factory workflow",
            recommended_workflow="Create an intent and start a review-first factory run.",
            requires_confirmation=True,
            command_tier="workflow",
            workflow_pack="intent_execution",
        )
    if _contains(lowered, "plan this", "create a plan", "make a plan", "plan for", "roadmap"):
        return RouteDecision(
            intent="plan_intent",
            confidence=0.82,
            reason="request asks for planning",
            recommended_workflow="Create an intent and draft a reviewable plan.",
            requires_confirmation=True,
            command_tier="workflow",
        )
    if _contains(
        lowered, "today", "morning", "work on today", "plan my day", "daily brief", "next action", "prioritize"
    ):
        return RouteDecision(
            intent="daily_brief",
            confidence=0.86,
            reason="request asks for daily prioritization",
            recommended_workflow="Summarize open work, approvals, and the highest-value next action.",
            workflow_pack="daily_ops",
        )
    if _contains(lowered, "find", "search", "context", "recall", "what do we know", "why"):
        return RouteDecision(
            intent="retrieve_context",
            confidence=0.78,
            reason="request asks for memory or context retrieval",
            recommended_workflow="Run GraphRAG-backed context retrieval.",
            command_tier="expert",
        )
    if _contains(lowered, "capture", "remember", "note", "task:", "todo", "follow up"):
        return RouteDecision(
            intent="capture",
            confidence=0.72,
            reason="request looks like a note or task capture",
            recommended_workflow="Capture the request into the inbox.",
        )
    return RouteDecision(
        intent="unknown",
        confidence=0.35,
        reason="no deterministic route matched",
        recommended_workflow="Use chat backend fallback or ask a clarifying question.",
        requires_confirmation=True,
    )


def _router_threshold() -> float:
    try:
        return float(os.getenv("MYOS_ROUTER_MIN_CONFIDENCE", "0.70"))
    except ValueError:
        return 0.70


def _coerce_model_decision(value: object, *, fallback: RouteDecision) -> RouteDecision:
    if not isinstance(value, dict):
        raise ValueError("router model response is not a JSON object")
    intent = str(value.get("intent") or "").strip()
    if intent not in ROUTABLE_INTENTS:
        raise ValueError(f"unsupported router intent: {intent}")
    confidence = float(value.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    workflow_pack = str(value.get("workflow_pack") or "").strip()
    if workflow_pack not in {"", "intent_execution", "daily_ops", "software_delivery", "connector_ops"}:
        workflow_pack = ""
    tier = str(value.get("command_tier") or fallback.command_tier).strip()
    if tier not in COMMAND_TIERS:
        tier = fallback.command_tier
    raw_confirmation = value.get("requires_confirmation", fallback.requires_confirmation)
    if isinstance(raw_confirmation, str):
        requires_confirmation = raw_confirmation.strip().lower() not in {"0", "false", "no", "off", ""}
    else:
        requires_confirmation = bool(raw_confirmation)
    return RouteDecision(
        intent=intent,
        confidence=confidence,
        reason=str(value.get("reason") or "tiny local router model").strip()[:300],
        recommended_workflow=str(value.get("recommended_workflow") or fallback.recommended_workflow).strip()[:500],
        requires_confirmation=requires_confirmation,
        proposed_actions=[],
        command_tier=tier,
        workflow_pack=workflow_pack,
        backend=os.getenv("MYOS_ROUTER_BACKEND", "command") or "command",
    )


def _model_route_text(text: str, *, surface: str, fallback: RouteDecision) -> RouteDecision:
    command = os.getenv("MYOS_ROUTER_COMMAND", "").strip()
    if not command:
        fallback.fallback_reason = "MYOS_ROUTER_COMMAND not configured"
        return fallback
    request = {
        "purpose": "router",
        "surface": surface,
        "text": text,
        "allowed_intents": list(ROUTABLE_INTENTS),
        "heuristic": fallback.to_dict(),
        "command_catalog": command_catalog(limit=32),
        "command_mapper": command_registry.local_model_command_mapper(),
    }
    started = time.monotonic()
    try:
        proc = subprocess.run(
            shlex.split(command),
            input=json.dumps(request, ensure_ascii=True),
            capture_output=True,
            text=True,
            timeout=int(os.getenv("MYOS_ROUTER_TIMEOUT_SEC", "8")),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"exit={proc.returncode}")[:300])
        parsed = json.loads(proc.stdout or "{}")
        decision = _coerce_model_decision(parsed, fallback=fallback)
        decision.fallback_reason = f"heuristic confidence {fallback.confidence:.2f}; model latency_ms={int((time.monotonic() - started) * 1000)}"
        return decision
    except Exception as exc:  # noqa: BLE001 - routing must never fail closed
        fallback.fallback_reason = f"router model fallback ignored: {str(exc)[:200]}"
        return fallback


def route_text(text: str, *, surface: str = "cli") -> RouteDecision:
    heuristic = _heuristic_route_text(text, surface=surface)
    if heuristic.confidence >= _router_threshold():
        return heuristic
    return _model_route_text(text, surface=surface, fallback=heuristic)


def _decision_for_intent(intent: str, *, source_feedback_id: int | None = None) -> RouteDecision:
    if intent == "approval_review":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Review pending approvals and recent execution receipts.",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    if intent == "system_health":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Run local schema and readiness checks.",
            command_tier="diagnostic",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    if intent == "connector_update":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Create approval-gated connector dry-run drafts.",
            requires_confirmation=True,
            command_tier="workflow",
            workflow_pack="connector_ops",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    if intent == "factory_run":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Create an intent and start a review-first factory run.",
            requires_confirmation=True,
            command_tier="workflow",
            workflow_pack="intent_execution",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    if intent == "plan_intent":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Create an intent and draft a reviewable plan.",
            requires_confirmation=True,
            command_tier="workflow",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    if intent == "daily_brief":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Summarize open work, approvals, and the highest-value next action.",
            workflow_pack="daily_ops",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    if intent == "retrieve_context":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Run GraphRAG-backed context retrieval.",
            command_tier="expert",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    if intent == "capture":
        return RouteDecision(
            intent=intent,
            confidence=1.0,
            reason="learned exact-match route correction",
            recommended_workflow="Capture the request into the inbox.",
            backend="feedback",
            fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
        )
    return RouteDecision(
        intent="unknown",
        confidence=1.0,
        reason="learned exact-match route correction",
        recommended_workflow="Use chat backend fallback or ask a clarifying question.",
        requires_confirmation=True,
        backend="feedback",
        fallback_reason=f"route_feedback#{source_feedback_id}" if source_feedback_id else "",
    )


def route_with_feedback(conn: sqlite3.Connection, text: str, *, surface: str = "cli") -> RouteDecision:
    text_hash = _text_hash(text)
    row = conn.execute(
        """
        SELECT expected_intent, source_feedback_id
        FROM route_overrides
        WHERE text_hash = ? AND status = 'active'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (text_hash,),
    ).fetchone()
    if row and row["expected_intent"] in ROUTABLE_INTENTS:
        decision = _decision_for_intent(row["expected_intent"], source_feedback_id=row["source_feedback_id"])
        decision.fallback_reason = (
            decision.fallback_reason + "; " if decision.fallback_reason else ""
        ) + "exact text_hash match"
        return decision
    return route_text(text, surface=surface)


def record_route_event(conn: sqlite3.Connection, text: str, *, surface: str, decision: RouteDecision) -> int:
    append_event(
        conn,
        "smart_route",
        "router",
        None,
        json.dumps(
            {
                "surface": surface,
                "intent": decision.intent,
                "confidence": decision.confidence,
                "backend": decision.backend,
                "fallback_reason": decision.fallback_reason,
                "text_hash": _text_hash(text),
            },
            ensure_ascii=True,
        ),
    )
    row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
    event_id = int(row["id"]) if row else 0
    observability.link_current_trace(
        conn,
        route_event_id=event_id,
        intent=decision.intent,
        command_tier=decision.command_tier,
    )
    return event_id


def choose_autopilot_workflow(signals: list[dict[str, Any]]) -> dict[str, str]:
    joined = " ".join(f"{s.get('title', '')} {s.get('detail', '')}" for s in signals).strip()
    decision = route_text(joined or "plan my day", surface="autopilot")
    pack = decision.workflow_pack or (
        "daily_ops" if decision.intent in {"daily_brief", "unknown"} else "intent_execution"
    )
    if decision.intent == "connector_update":
        pack = "connector_ops"
    return {"intent": decision.intent, "workflow_pack": pack, "reason": decision.reason}


def _top_open_items(conn: sqlite3.Connection, limit: int = 3) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, title, kind, risk_score, due_date, owner
            FROM work_items
            WHERE status='open'
            ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC, id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    ]


def _latest_intent_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM intents WHERE status='open' ORDER BY priority ASC, id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def _connector_from_text(text: str) -> str:
    lowered = text.lower()
    for connector in ("jira", "github", "confluence", "aha"):
        if connector in lowered:
            return connector
    return "jira"


def autonomy_decision_for_route(conn: sqlite3.Connection, decision: RouteDecision) -> dict[str, Any]:
    if decision.intent in {"daily_brief", "retrieve_context", "approval_review", "system_health"}:
        safety = "read_only"
        requires_confirmation = False
    elif decision.intent in {"connector_update", "factory_run", "unknown"}:
        safety = "approval_gated"
        requires_confirmation = True
    else:
        safety = "local_write"
        requires_confirmation = False
    return autonomy.decide_command(
        "do",
        safety=safety,
        requires_confirmation=requires_confirmation,
        level=autonomy.level_from_policy(conn),
        requested_mode=decision.workflow_pack,
    )


def execute_route(
    conn: sqlite3.Connection, text: str, *, surface: str = "do", decision: RouteDecision | None = None
) -> dict[str, Any]:
    decision = decision or route_with_feedback(conn, text, surface=surface)
    result: dict[str, Any] = {"decision": decision.to_dict(), "actions": []}
    if decision.intent == "capture":
        inbox_id, created = agentcore.capture_item(conn, text=text, kind="task", source=f"smart_{surface}")
        result.update({"status": "captured" if created else "duplicate", "inbox_id": inbox_id})
    elif decision.intent == "daily_brief":
        approvals = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_actions WHERE requires_approval=1 AND status IN ('proposed', 'approved')"
        ).fetchone()["c"]
        result.update(
            {"status": "summarized", "open_items": _top_open_items(conn), "pending_approvals": int(approvals)}
        )
    elif decision.intent == "retrieve_context":
        hits = graphrag.retrieve(conn, text, limit=5, record_run=True, mode=f"smart_{surface}")
        result.update({"status": "retrieved", "hits": hits[:5]})
    elif decision.intent == "approval_review":
        actions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, action_type, title, status
                FROM agent_actions
                WHERE requires_approval=1 AND status IN ('proposed', 'approved')
                ORDER BY created_at ASC
                LIMIT 5
                """
            ).fetchall()
        ]
        receipts = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, agent_action_id, final_status, follow_up_required
                FROM action_execution_receipts
                ORDER BY created_at DESC, id DESC
                LIMIT 5
                """
            ).fetchall()
        ]
        result.update({"status": "reviewed", "pending_actions": actions, "recent_receipts": receipts})
    elif decision.intent == "system_health":
        health = verify_schema(conn)
        result.update({"status": "checked", "schema_ok": bool(health.get("ok")), "schema": health})
    elif decision.intent == "plan_intent":
        intent_id = intents.create_intent(conn, objective=text, context=f"Created by smart router from {surface}.")
        plan_id = plans.create_plan(conn, intent_id=intent_id)
        result.update({"status": "planned", "intent_id": intent_id, "plan_id": plan_id})
    elif decision.intent in {"factory_run", "connector_update"}:
        intent_id = _latest_intent_id(conn)
        if intent_id is None:
            intent_id = intents.create_intent(conn, objective=text, context=f"Created by smart router from {surface}.")
        if decision.intent == "connector_update":
            connector = _connector_from_text(text)
            factory.set_policy(
                conn, allowed_mode="review_first", connector=connector, action_type="draft_external_update"
            )
        run = factory.start_review_first_run(
            conn,
            intent_id=intent_id,
            mode="review_first",
            workflow_pack=decision.workflow_pack or "intent_execution",
        )
        observability.link_current_trace(conn, factory_run_id=run["id"])
        result.update(
            {
                "status": "factory_started",
                "intent_id": intent_id,
                "factory_run_id": run["id"],
                "factory_status": run["status"],
            }
        )
    else:
        result.update({"status": "needs_clarification", "message": decision.recommended_workflow})
    record_route_event(conn, text, surface=surface, decision=decision)
    return result


def summarize_result(result: dict[str, Any]) -> str:
    decision = result["decision"]
    lines = [
        f"Smart route: {decision['intent']} confidence={decision['confidence']:.2f}",
        f"Reason: {decision['reason']}",
        f"Workflow: {decision['recommended_workflow']}",
        f"Status: {result.get('status', 'unknown')}",
    ]
    if result.get("inbox_id"):
        lines.append(f"Inbox item: #{result['inbox_id']}")
    if result.get("intent_id"):
        lines.append(f"Intent: #{result['intent_id']}")
    if result.get("plan_id"):
        lines.append(f"Plan: #{result['plan_id']}")
    if result.get("factory_run_id"):
        lines.append(f"Factory run: #{result['factory_run_id']} status={result.get('factory_status')}")
    if result.get("pending_approvals") is not None:
        lines.append(f"Pending approvals: {result['pending_approvals']}")
    for item in result.get("open_items", [])[:3]:
        lines.append(f"- work_item#{item['id']} risk={item['risk_score']} {item['title']}")
    for hit in result.get("hits", [])[:3]:
        citation = hit.get("citation") or f"{hit.get('source_type')}#{hit.get('source_id')}"
        lines.append(f"- {citation}: {str(hit.get('content', ''))[:120]}")
    if result.get("pending_actions"):
        lines.append("Pending actions:")
        for action in result["pending_actions"][:3]:
            lines.append(
                f"- action #{action['id']} [{action['action_type']}] {action['title']} status={action['status']}"
            )
    return "\n".join(lines)


def load_route_eval_fixtures(path: str | Path | None = None) -> list[dict[str, Any]]:
    fixture_path = Path(path).expanduser() if path else DEFAULT_EVAL_FIXTURE_PATH
    raw = json.loads(fixture_path.read_text())
    if not isinstance(raw, list):
        raise ValueError("route eval fixture must be a JSON list")
    fixtures: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"route eval fixture #{idx} must be an object")
        fixture_id = str(item.get("id") or f"case_{idx}").strip()
        text = str(item.get("text") or "").strip()
        expected = str(item.get("expected_intent") or "").strip()
        if not text or expected not in ROUTABLE_INTENTS:
            raise ValueError(f"route eval fixture {fixture_id} has invalid text or expected_intent")
        fixtures.append(
            {
                "id": fixture_id,
                "text": text,
                "expected_intent": expected,
                "expected_workflow_pack": str(item.get("expected_workflow_pack") or "").strip(),
                "category": str(item.get("category") or "general").strip(),
            }
        )
    return fixtures


def _score_route_case(fixture: dict[str, Any], decision: RouteDecision) -> dict[str, Any]:
    expected_pack = fixture.get("expected_workflow_pack") or ""
    intent_match = decision.intent == fixture["expected_intent"]
    workflow_match = not expected_pack or decision.workflow_pack == expected_pack
    return {
        "fixture_id": fixture["id"],
        "category": fixture["category"],
        "text_hash": _text_hash(fixture["text"]),
        "expected_intent": fixture["expected_intent"],
        "expected_workflow_pack": expected_pack,
        "actual_intent": decision.intent,
        "actual_workflow_pack": decision.workflow_pack,
        "confidence": decision.confidence,
        "backend": decision.backend,
        "intent_match": intent_match,
        "workflow_match": workflow_match,
        "passed": intent_match and workflow_match,
        "reason": decision.reason,
    }


def _eval_summary(cases: list[dict[str, Any]], *, model_shadow: bool) -> dict[str, Any]:
    total = len(cases)
    passed = sum(1 for case in cases if case["passed"])
    low_confidence = sum(1 for case in cases if float(case["confidence"]) < _router_threshold())
    by_intent: dict[str, dict[str, int]] = {}
    model_overrides = 0
    model_wins = 0
    model_losses = 0
    for case in cases:
        bucket = by_intent.setdefault(case["expected_intent"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if case["passed"] else 0
        shadow = case.get("model_shadow")
        if isinstance(shadow, dict) and shadow.get("backend") != "heuristic":
            if shadow.get("actual_intent") != case["actual_intent"]:
                model_overrides += 1
            if shadow.get("passed") and not case["passed"]:
                model_wins += 1
            if case["passed"] and not shadow.get("passed"):
                model_losses += 1
    if model_shadow and model_wins > model_losses:
        calibration = "model shadow is helping; consider raising MYOS_ROUTER_MIN_CONFIDENCE slightly"
    elif model_shadow and model_losses > model_wins:
        calibration = "model shadow is hurting; keep deterministic threshold lower or inspect the model prompt"
    elif low_confidence:
        calibration = "low-confidence cases exist; review fixture failures before changing threshold"
    else:
        calibration = "routing confidence looks stable; keep current threshold"
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": (passed / total) if total else 0.0,
        "low_confidence": low_confidence,
        "model_shadow": model_shadow,
        "model_overrides": model_overrides,
        "model_wins": model_wins,
        "model_losses": model_losses,
        "by_intent": by_intent,
        "calibration": calibration,
    }


def evaluate_routes(*, fixture_path: str | Path | None = None, model_shadow: bool = False) -> dict[str, Any]:
    fixtures = load_route_eval_fixtures(fixture_path)
    cases: list[dict[str, Any]] = []
    for fixture in fixtures:
        deterministic = _heuristic_route_text(fixture["text"], surface="eval")
        case = _score_route_case(fixture, deterministic)
        if model_shadow:
            shadow_fallback = _heuristic_route_text(fixture["text"], surface="eval_shadow")
            shadow_decision = _model_route_text(fixture["text"], surface="eval_shadow", fallback=shadow_fallback)
            shadow_case = _score_route_case(fixture, shadow_decision)
            shadow_case["fallback_reason"] = shadow_decision.fallback_reason
            case["model_shadow"] = shadow_case
        cases.append(case)
    return {
        "fixture_path": str(Path(fixture_path).expanduser() if fixture_path else DEFAULT_EVAL_FIXTURE_PATH),
        "summary": _eval_summary(cases, model_shadow=model_shadow),
        "cases": cases,
    }


def record_route_eval(conn: sqlite3.Connection, eval_result: dict[str, Any]) -> int:
    summary = eval_result["summary"]
    cur = conn.execute(
        """
        INSERT INTO route_eval_runs (
            fixture_path, total_cases, passed_cases, accuracy, low_confidence_cases,
            model_shadow, model_overrides, calibration
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eval_result["fixture_path"],
            summary["total"],
            summary["passed"],
            summary["accuracy"],
            summary["low_confidence"],
            1 if summary["model_shadow"] else 0,
            summary["model_overrides"],
            summary["calibration"],
        ),
    )
    run_id = int(cur.lastrowid)
    for case in eval_result["cases"]:
        shadow = case.get("model_shadow") if isinstance(case.get("model_shadow"), dict) else {}
        conn.execute(
            """
            INSERT INTO route_eval_cases (
                route_eval_run_id, fixture_id, category, text_hash, expected_intent,
                actual_intent, backend, confidence, passed, shadow_intent, shadow_backend,
                shadow_passed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                case["fixture_id"],
                case["category"],
                case["text_hash"],
                case["expected_intent"],
                case["actual_intent"],
                case["backend"],
                case["confidence"],
                1 if case["passed"] else 0,
                shadow.get("actual_intent", ""),
                shadow.get("backend", ""),
                1 if shadow.get("passed") else 0,
            ),
        )
    conn.commit()
    return run_id


def record_route_feedback(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    expected_intent: str,
    note: str = "",
) -> int:
    if expected_intent not in ROUTABLE_INTENTS:
        raise ValueError(f"unsupported expected intent: {expected_intent}")
    event = conn.execute(
        """
        SELECT id, payload
        FROM event_log
        WHERE id = ? AND event_type = 'smart_route'
        """,
        (int(event_id),),
    ).fetchone()
    if not event:
        raise ValueError(f"smart route event not found: {event_id}")
    payload = json.loads(event["payload"] or "{}")
    text_hash = str(payload.get("text_hash") or "").strip()
    note_hash = _text_hash(note) if note else ""
    cur = conn.execute(
        """
        INSERT INTO route_feedback (
            event_log_id, surface, expected_intent, actual_intent, backend, confidence,
            note_hash, note_length, text_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(event["id"]),
            str(payload.get("surface") or ""),
            expected_intent,
            str(payload.get("intent") or ""),
            str(payload.get("backend") or ""),
            float(payload.get("confidence") or 0.0),
            note_hash,
            len(note or ""),
            text_hash,
        ),
    )
    feedback_id = int(cur.lastrowid)
    if text_hash:
        conn.execute(
            """
            INSERT INTO route_overrides (
                text_hash, expected_intent, source_feedback_id, status, updated_at
            )
            VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP)
            ON CONFLICT(text_hash) DO UPDATE SET
                expected_intent=excluded.expected_intent,
                source_feedback_id=excluded.source_feedback_id,
                status='active',
                updated_at=CURRENT_TIMESTAMP
            """,
            (text_hash, expected_intent, feedback_id),
        )
    conn.commit()
    return feedback_id


def list_route_overrides(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, text_hash, expected_intent, source_feedback_id, status, created_at, updated_at
            FROM route_overrides
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    ]
