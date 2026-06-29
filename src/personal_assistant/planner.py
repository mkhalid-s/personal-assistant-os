"""The reasoning seam used by delegate/autopilot: analogy retrieval, the
deterministic keyword planner, and `_ai_reason_artifacts` (which routes to a
pluggable backend or an external `MYOS_AI_COMMAND`, falling back to the keyword
planner). Extracted from cli.py (refactor #12).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

from . import providers
from .inbox import infer_kind
from .privacy import apply_privacy_filters, get_policy_map, redact_obj
from .retrieval import hybrid_score


def _agent_analogies(conn, query: str, limit: int = 5) -> list[tuple[float, str, str]]:
    candidates: list[tuple[str, str]] = []
    work_rows = conn.execute(
        """
        SELECT id, title, kind, risk_score, status
        FROM work_items
        ORDER BY updated_at DESC
        LIMIT 200
        """
    ).fetchall()
    for row in work_rows:
        candidates.append(
            (
                f"work_item#{row['id']}",
                f"{row['title']} kind={row['kind']} risk={row['risk_score']} status={row['status']}",
            )
        )
    obs_rows = conn.execute(
        """
        SELECT observation_type, content
        FROM agent_observations
        ORDER BY created_at DESC
        LIMIT 100
        """
    ).fetchall()
    for row in obs_rows:
        candidates.append((f"observation:{row['observation_type']}", row["content"]))

    scored: list[tuple[float, str, str]] = []
    for source, content in candidates:
        score = hybrid_score(query, content)
        if score > 0:
            scored.append((score, source, content))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


def _agent_plan(objective: str, context: str, analogy_count: int) -> list[dict[str, str]]:
    text = f"{objective} {context}".lower()
    steps = [
        {"step": "clarify_outcome", "detail": "State the desired outcome and success signal."},
        {"step": "gather_context", "detail": "Pull relevant prior work, risks, decisions, and people context."},
    ]
    if any(k in text for k in ["risk", "blocked", "blocker", "dependency", "incident"]):
        steps.append({"step": "reduce_risk", "detail": "Identify owner, deadline pressure, and renegotiation path."})
    if any(k in text for k in ["meeting", "zoom", "discussion", "notes", "transcript"]):
        steps.append({"step": "summarize_discussion", "detail": "Extract decisions, follow-ups, owners, and due dates."})
    if any(k in text for k in ["jira", "github", "pr", "ticket", "aha", "confluence"]):
        steps.append({"step": "prepare_system_update", "detail": "Draft external-system update for approval."})
    if analogy_count:
        steps.append({"step": "reuse_playbook", "detail": f"Compare with {analogy_count} analogous prior items."})
    steps.append({"step": "propose_actions", "detail": "Create approval-gated actions and safe local next steps."})
    return steps


def _agent_action_specs(objective: str, context: str, plan: list[dict[str, str]]) -> list[dict[str, object]]:
    text = f"{objective} {context}"
    lower = text.lower()
    actions: list[dict[str, object]] = [
        {
            "action_type": "create_inbox_item",
            "title": "Capture assistant-generated next action",
            "payload": {
                "text": f"Assistant follow-up: {objective}",
                "kind": infer_kind(objective),
                "source": "agent",
            },
            "requires_approval": 0,
        }
    ]
    if any(k in lower for k in ["risk", "blocked", "blocker", "dependency"]):
        actions.append(
            {
                "action_type": "draft_message",
                "title": "Draft risk/renegotiation message",
                "payload": {
                    "draft": (
                        "This item looks at risk. Proposed next step: confirm owner, deadline, "
                        "and whether to reduce scope or extend timeline."
                    )
                },
                "requires_approval": 1,
            }
        )
    if any(k in lower for k in ["jira", "github", "pr", "ticket", "aha", "confluence"]):
        actions.append(
            {
                "action_type": "draft_external_update",
                "title": "Draft external system update",
                "payload": {
                    "draft": f"Status update drafted from assistant objective: {objective}",
                    "target": "external_system",
                },
                "requires_approval": 1,
            }
        )
    if any(step["step"] == "summarize_discussion" for step in plan):
        actions.append(
            {
                "action_type": "draft_summary",
                "title": "Draft discussion summary",
                "payload": {"draft": f"Summary seed: {context or objective}"},
                "requires_approval": 1,
            }
        )
    return actions


def _normalize_ai_plan(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        step = str(item.get("step", "")).strip()
        detail = str(item.get("detail", "")).strip()
        if step and detail:
            normalized.append({"step": step[:80], "detail": detail[:300]})
    return normalized[:10]


def _normalize_ai_actions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("action_type", "")).strip()
        title = str(item.get("title", "")).strip()
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        raw_approval = item.get("requires_approval", 1)
        if isinstance(raw_approval, bool):
            requires_approval = 1 if raw_approval else 0
        elif raw_approval is None:
            requires_approval = 1
        elif isinstance(raw_approval, str):
            requires_approval = 0 if raw_approval.strip().lower() in {"0", "false", "no", "off"} else 1
        else:
            requires_approval = 1 if int(raw_approval) else 0
        if action_type and title:
            # Only local bookkeeping actions can bypass approval; external/draft actions
            # stay human-gated even if an AI provider labels them safe.
            if action_type != "create_inbox_item":
                requires_approval = 1
            normalized.append(
                {
                    "action_type": action_type[:80],
                    "title": title[:180],
                    "payload": payload,
                    "requires_approval": requires_approval,
                }
            )
    return normalized[:10]


def _ai_reason_artifacts(
    conn,
    *,
    objective: str,
    context: str,
    analogies: list[tuple[float, str, str]],
    purpose: str,
) -> tuple[list[dict[str, str]], list[dict[str, object]], str]:
    policy = get_policy_map(conn)
    provider = os.getenv("MYOS_AI_PROVIDER") or policy.get("ai_provider", "local")
    command = os.getenv("MYOS_AI_COMMAND", "").strip()
    if not command:
        # Pluggable backend (claude/copilot/cursor) before the keyword fallback.
        backend_name = (
            os.getenv("MYOS_AGENT_BACKEND")
            or os.getenv("MYOS_AI_PROVIDER")
            or policy.get("ai_provider", "")
        ).strip().lower()
        if backend_name in ("", "local") and os.getenv("ANTHROPIC_API_KEY", "").strip():
            backend_name = "claude"
        if backend_name in ("claude", "anthropic", "copilot", "cursor"):
            try:
                backend = providers.get_backend("claude" if backend_name == "anthropic" else backend_name)
                result = backend.reason(
                    conn,
                    {
                        "purpose": purpose,
                        "objective": objective,
                        "context": context,
                        "analogies": [
                            {"score": s, "source": src, "content": apply_privacy_filters(conn, c)}
                            for s, src, c in analogies[:5]
                        ],
                    },
                )
                plan = _normalize_ai_plan(result.get("plan"))
                actions = _normalize_ai_actions(result.get("actions"))
                if plan and actions:
                    return plan, actions, backend.name
            except Exception:
                pass  # degrade to the deterministic keyword planner below
        plan = _agent_plan(objective, context, len(analogies))
        return plan, _agent_action_specs(objective, context, plan), "local"

    request = {
        "purpose": purpose,
        "objective": objective,
        "context": context,
        "analogies": [
            {"score": score, "source": source, "content": apply_privacy_filters(conn, content)}
            for score, source, content in analogies[:5]
        ],
        "instructions": (
            "Return JSON with keys plan and actions. plan is a list of {step, detail}. "
            "actions is a list of {action_type, title, payload, requires_approval}. "
            "Only mark requires_approval=0 for safe local bookkeeping actions."
        ),
    }
    started = time.monotonic()
    status = "error"
    response_json = ""
    error = ""
    try:
        proc = subprocess.run(
            shlex.split(command),
            input=json.dumps(request, ensure_ascii=True),
            capture_output=True,
            text=True,
            timeout=int(policy.get("ai_timeout_sec", "20")),
            check=False,
        )
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout or f"exit={proc.returncode}")[:1000]
            raise RuntimeError(error)
        parsed = json.loads(proc.stdout)
        plan = _normalize_ai_plan(parsed.get("plan"))
        actions = _normalize_ai_actions(parsed.get("actions"))
        if not plan or not actions:
            raise ValueError("AI response missing valid plan/actions")
        status = "ok"
        response_json = json.dumps(parsed, ensure_ascii=True)[:8000]
        return plan, actions, provider
    except Exception as exc:
        error = str(exc)[:1000]
        plan = _agent_plan(objective, context, len(analogies))
        return plan, _agent_action_specs(objective, context, plan), "local_fallback"
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        # Redact before persisting (finding #6, mirroring agent_cli._audit): response_json is
        # freeform stdout from an arbitrary external MYOS_AI_COMMAND agent and `error` is its
        # stderr — both can echo back the user's text or provider-side secrets/PII. request is
        # redacted via redact_obj so the row is defended even if a caller forgets to pre-filter.
        conn.execute(
            """
            INSERT INTO ai_provider_calls (provider, purpose, status, request_json, response_json, error, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                purpose,
                status,
                json.dumps(redact_obj(conn, request), ensure_ascii=True)[:8000],
                apply_privacy_filters(conn, response_json)[:8000],
                apply_privacy_filters(conn, error)[:1000],
                latency_ms,
            ),
        )
