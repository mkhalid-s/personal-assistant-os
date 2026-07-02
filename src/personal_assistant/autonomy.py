"""Graded autonomy: classify every candidate action / tool call into a tier.

    auto    -> execute immediately, no confirmation
    confirm -> execute after one-tap confirmation (queued when unattended)
    blocked -> never auto; destructive / major / irreversible — explicit human action only

A hard destructive guard sits ABOVE the autonomy level: no level (not even ``bold``)
can promote a destructive action to ``auto`` or let it run unattended.

These functions are pure (no DB import) so both the queue executor (`cli`) and the
Agent SDK ``can_use_tool`` callback can share one policy without an import cycle.
Callers pass the active ``level`` (read from the ``assistant_policies`` table).
"""

from __future__ import annotations

import hashlib
import re

AUTO = "auto"
CONFIRM = "confirm"
BLOCKED = "blocked"
LEVELS = ("safe", "balanced", "bold")
DEFAULT_LEVEL = "balanced"
DECISIONS = ("allowed", "needs_approval", BLOCKED)
COMMAND_DECISION_FIXTURES = (
    {
        "id": "read_context",
        "command": "context",
        "safety": "read_only",
        "requires_confirmation": False,
        "expected_decision": "allowed",
    },
    {
        "id": "local_capture",
        "command": "capture",
        "safety": "local_write",
        "requires_confirmation": False,
        "expected_decision": "allowed",
    },
    {
        "id": "diagnostic_release",
        "command": "release-check",
        "safety": "diagnostic",
        "requires_confirmation": False,
        "expected_decision": "allowed",
    },
    {
        "id": "external_sync",
        "command": "sync",
        "safety": "external_write",
        "requires_confirmation": True,
        "expected_decision": "needs_approval",
    },
    {
        "id": "factory_start",
        "command": "factory",
        "safety": "approval_gated",
        "requires_confirmation": True,
        "expected_decision": "needs_approval",
    },
    {
        "id": "unknown_destructive",
        "command": "delete-everything",
        "safety": "unknown",
        "requires_confirmation": True,
        "expected_decision": BLOCKED,
    },
)

# Base tier for known *queued action types* (the things that flow through
# agent_actions). Only create_inbox_item is AUTO — and AUTO_ACTION_TYPES below is
# the single source of truth that agentcore.enqueue_proposal consumes, so the
# tier table and the real auto-exec gate cannot drift (review finding #11).
_ACTION_TIER = {
    "create_inbox_item": AUTO,
    "draft_external_update": CONFIRM,   # post Jira/GitHub/Slack comment, generic external draft
    "apply_patch": CONFIRM,
    "create_issue": CONFIRM,
    "update_issue": CONFIRM,
    "create_calendar_event": CONFIRM,
    "send_message": CONFIRM,
}

# Action types that may execute without approval. Derived from the tier table so
# there is exactly one definition of "safe to auto-run".
AUTO_ACTION_TYPES = frozenset(at for at, tier in _ACTION_TIER.items() if tier == AUTO)

# Substrings in an action_type / op name that are ALWAYS destructive -> blocked.
_DESTRUCTIVE_HINTS = (
    "delete", "destroy", "drop", "purge", "wipe", "bulk", "mass",
    "force", "deploy", "prod", "close_all", "remove_branch", "reset_hard",
    "revoke", "truncate", "shred", "overwrite", "rmdir", "uninstall",
)

# Confirm-tier types the ``bold`` level may auto-run. Deliberately EMPTY (review
# finding #2): no external mutation may ever auto-send without a human tap, at any
# level. `bold` only changes prompts/latency elsewhere, never the send gate. Add a
# type here ONLY if it is purely local and fully reversible.
_BOLD_AUTO: frozenset = frozenset()

# Read-only tool/op names (Agent SDK + MYOS) -> auto.
_READ_TOOLS = {
    "read", "grep", "glob", "ls", "web_search", "web_fetch", "recall",
    "get_brief", "list_at_risk", "list_waiting_on", "query_context",
    "get_today", "risk_radar", "why_item", "metrics",
}
_READ_HINTS = ("get", "list", "search", "read", "fetch", "view", "grep", "glob", "recall", "describe", "show")
_READ_TOKENS = frozenset(_READ_HINTS)  # token-exact membership (see classify_tool step 4)
_WRITE_HINTS = ("write", "edit", "create", "update", "comment", "post", "send", "add", "apply", "set")

# Dangerous shell/VCS/infra patterns for free-form bash tool calls. Matched against
# a normalized command (see _normalize_cmd) so $IFS / quote-splitting evasions
# (review finding #3) don't slip through.
_DANGEROUS_BASH = [
    r"\brm\b", r"\bunlink\b", r"\beval\b",  # any rm/unlink (bare or flagged, flag-after-path) + eval (A4)
    r"\bfind\b.*-delete", r"\brmdir\b", r"\bshred\b", r"\btruncate\b",
    r"\bgit\s+push\b.*(--force|-f\b|\s\+)",  # incl. refspec '+main' force
    r"\bgit\s+reset\s+--hard", r"\bgit\s+clean\b", r"\bgit\s+branch\s+-D\b",
    r"\bDROP\s+(TABLE|DATABASE)\b", r"\bmkfs\b", r":\(\)\s*\{", r"\bshutdown\b",
    r"\breboot\b", r"\bdd\s+if=", r">\s*/dev/sd", r"\bchmod\s+-?R?\s*777",
    r"\bsudo\b", r"\bkubectl\s+delete\b", r"\bterraform\s+(destroy|apply)\b",
    r"\bnpm\s+publish\b", r"\|\s*(sudo\s+)?(ba)?sh\b", r"base64\b.*\|\s*(ba)?sh",
    r"rmtree", r"os\.remove", r"os\.unlink",
]


def _norm_level(level: str | None) -> str:
    return level if level in LEVELS else DEFAULT_LEVEL


def _is_destructive_payload(payload: dict | None) -> bool:
    payload = payload or {}
    if payload.get("destructive") is True:
        return True
    for key in ("targets", "recipients", "issue_keys", "channels"):
        val = payload.get(key)
        if isinstance(val, (list, tuple)) and len(val) > 5:  # fan-out / mass action
            return True
    return False


def classify_action(action_type: str, payload: dict | None = None, *, level: str = DEFAULT_LEVEL) -> dict:
    """Classify a queued ``agent_action`` (action_type + payload)."""
    level = _norm_level(level)
    at = (action_type or "").lower()
    if any(h in at for h in _DESTRUCTIVE_HINTS) or _is_destructive_payload(payload):
        return {"tier": BLOCKED, "destructive": True, "reason": f"destructive action '{action_type}'"}
    base = _ACTION_TIER.get(at, CONFIRM)  # unknown -> confirm (safe default)
    tier = base
    if level == "bold" and base == CONFIRM and at in _BOLD_AUTO:
        tier = AUTO
    return {"tier": tier, "destructive": False, "reason": f"{action_type} -> {tier} (level={level})"}


def classify_tool(tool_name: str, tool_input: dict | None = None, *, level: str = DEFAULT_LEVEL) -> dict:
    """Classify a live Agent-SDK tool call (built-in tool or mcp__server__op)."""
    level = _norm_level(level)
    name = (tool_name or "").lower()
    op = name.split("__")[-1] if name.startswith("mcp__") else name
    tool_input = tool_input or {}

    # 1. Bash: inspect the NORMALIZED command against the danger list.
    if op == "bash":
        cmd = _normalize_cmd(str(tool_input.get("command", "")))
        for pat in _DANGEROUS_BASH:
            if re.search(pat, cmd, re.IGNORECASE):
                return {"tier": BLOCKED, "destructive": True, "reason": f"dangerous command (/{pat}/)"}
        return {"tier": CONFIRM, "destructive": False, "reason": "bash command (side effects)"}

    # 2. Destructive-looking op names are blocked regardless of level.
    if any(h in op for h in _DESTRUCTIVE_HINTS):
        return {"tier": BLOCKED, "destructive": True, "reason": f"destructive op '{op}'"}

    # 3. Writes/mutations -> confirm. Checked BEFORE read so an op like
    #    'create_or_get_x' (contains the read token 'get') doesn't auto-run.
    if any(h in op for h in _WRITE_HINTS):
        return {"tier": CONFIRM, "destructive": False, "reason": "write/mutation tool"}

    # 4. Read-only -> auto ONLY when the LEADING verb is a read verb (review A2).
    #    Writes/destructive are already handled above; requiring the first token to be
    #    a read verb stops 'do_x_then_view'/'read_receipt_mark' from auto-running just
    #    because a read-ish token appears somewhere in the name.
    op_seq = [t for t in re.split(r"[^a-z0-9]+", op) if t]
    if name in _READ_TOOLS or op in _READ_TOOLS or (op_seq and op_seq[0] in _READ_TOKENS):
        return {"tier": AUTO, "destructive": False, "reason": "read-only tool"}

    # 5. Unknown -> confirm (never silently auto).
    return {"tier": CONFIRM, "destructive": False, "reason": "unknown tool, default confirm"}


def decide_command(
    command: str,
    *,
    safety: str = "",
    requires_confirmation: bool = False,
    level: str = DEFAULT_LEVEL,
    requested_mode: str = "",
) -> dict:
    """Explain the autonomy decision for a top-level MYOS command.

    This is intentionally small and advisory for normal local work. The hard
    execution guard remains `classify_action` / `classify_tool`; this function
    gives users and higher-level flows a consistent, pre-work explanation.
    """
    level = _norm_level(level)
    command_name = (command or "").strip().lower()
    safety = (safety or "").strip()
    requested_mode = (requested_mode or "").strip()
    if not command_name:
        return {
            "decision": BLOCKED,
            "tier": BLOCKED,
            "requires_approval": True,
            "reason": "missing command",
            "level": level,
            "safety": safety or "unknown",
        }
    if safety == "unknown" or (not safety and any(h in command_name for h in _DESTRUCTIVE_HINTS)):
        return {
            "decision": BLOCKED,
            "tier": BLOCKED,
            "requires_approval": True,
            "reason": f"unknown or destructive-looking command '{command_name}'",
            "level": level,
            "safety": safety or "unknown",
        }
    if safety in {"read_only", "diagnostic"}:
        return {
            "decision": "allowed",
            "tier": AUTO,
            "requires_approval": False,
            "reason": f"{safety} command allowed at autonomy_level={level}",
            "level": level,
            "safety": safety,
        }
    if safety == "local_write" and not requires_confirmation:
        return {
            "decision": "allowed",
            "tier": AUTO,
            "requires_approval": False,
            "reason": f"local write command allowed; external effects remain gated (level={level})",
            "level": level,
            "safety": safety,
        }
    if safety in {"approval_gated", "external_write"} or requires_confirmation:
        suffix = f"; requested_mode={requested_mode}" if requested_mode else ""
        return {
            "decision": "needs_approval",
            "tier": CONFIRM,
            "requires_approval": True,
            "reason": f"{safety or 'command'} requires review/approval before risky effects{suffix}",
            "level": level,
            "safety": safety or "unknown",
        }
    return {
        "decision": "needs_approval",
        "tier": CONFIRM,
        "requires_approval": True,
        "reason": f"unrecognized command safety '{safety or 'unknown'}', defaulting to approval",
        "level": level,
        "safety": safety or "unknown",
    }


def recommend_next_steps(
    decision: dict,
    *,
    command: str = "",
    intent: str = "",
    workflow_pack: str = "",
    factory_run_id: int | None = None,
) -> list[dict]:
    """Return deterministic, read-only next-step guidance for an autonomy decision."""
    decision_name = str(decision.get("decision") or "")
    safety = str(decision.get("safety") or "")
    command_name = (command or "").strip()
    intent = (intent or "").strip()
    workflow_pack = (workflow_pack or "").strip()
    if decision_name == "allowed":
        if command_name == "do" and intent in {"capture", "plan_intent"}:
            return [
                {
                    "label": "continue",
                    "command": "",
                    "reason": "Proceed with the local routed workflow; external effects remain gated.",
                }
            ]
        return []
    if decision_name == "needs_approval":
        if command_name == "factory" or intent == "factory_run":
            review = f"myos factory review --id {factory_run_id}" if factory_run_id else "myos factory review --id <run_id>"
            return [
                {
                    "label": "review_factory",
                    "command": review,
                    "reason": "Review the generated packet before approving execution.",
                }
            ]
        if intent == "connector_update" or workflow_pack == "connector_ops" or safety == "external_write":
            return [
                {
                    "label": "review_approvals",
                    "command": "myos approve --list",
                    "reason": "Inspect approval-gated connector actions before anything is sent.",
                }
            ]
        return [
            {
                "label": "inspect_gated_commands",
                "command": "myos router commands --safety approval_gated",
                "reason": "Review available approval-gated workflows before continuing.",
            }
        ]
    if decision_name == BLOCKED:
        return [
            {
                "label": "inspect_safe_commands",
                "command": "myos help diagnostic",
                "reason": "Use read-only diagnostics instead of a blocked or unknown operation.",
            },
            {
                "label": "inspect_recent_traces",
                "command": "myos trace list",
                "reason": "Review recent local activity before choosing a safer command.",
            },
        ]
    return []


def recommendation_key(step: dict) -> str:
    label = str(step.get("label") or "").strip()
    command = str(step.get("command") or "").strip()
    base = f"{label}|{command}"
    return _text_hash(base)[:24]


def _feedback_scores(conn) -> dict[str, int]:
    try:
        rows = conn.execute(
            """
            SELECT recommendation_key,
                   SUM(CASE WHEN useful = 1 THEN 1 ELSE -1 END) AS score
            FROM recommendation_feedback
            GROUP BY recommendation_key
            """
        ).fetchall()
    except Exception:
        return {}
    return {str(row["recommendation_key"]): int(row["score"] or 0) for row in rows}


def ranked_recommendations(conn, steps: list[dict]) -> list[dict]:
    scores = _feedback_scores(conn)
    ranked: list[tuple[int, int, dict]] = []
    for index, step in enumerate(steps):
        key = recommendation_key(step)
        enriched = dict(step)
        enriched["key"] = key
        enriched["score"] = scores.get(key, 0)
        ranked.append((-int(enriched["score"]), index, enriched))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def record_recommendation_feedback(
    conn,
    *,
    label: str,
    command: str = "",
    decision: str = "",
    intent: str = "",
    workflow_pack: str = "",
    useful: bool,
    note: str = "",
) -> int:
    if not label.strip():
        raise ValueError("recommendation label is required")
    step = {"label": label.strip(), "command": command.strip()}
    cur = conn.execute(
        """
        INSERT INTO recommendation_feedback (
            recommendation_key, label, command, decision, intent, workflow_pack,
            useful, note_hash, note_length
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recommendation_key(step),
            label.strip()[:120],
            command.strip()[:300],
            decision.strip()[:80],
            intent.strip()[:80],
            workflow_pack.strip()[:120],
            1 if useful else 0,
            _text_hash(note) if note else "",
            len(note or ""),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def recommendation_feedback_summary(conn, *, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT recommendation_key, label, command,
               SUM(CASE WHEN useful = 1 THEN 1 ELSE 0 END) AS useful_count,
               SUM(CASE WHEN useful = 0 THEN 1 ELSE 0 END) AS not_useful_count,
               SUM(CASE WHEN useful = 1 THEN 1 ELSE -1 END) AS score,
               MAX(created_at) AS last_feedback_at
        FROM recommendation_feedback
        GROUP BY recommendation_key, label, command
        ORDER BY score DESC, last_feedback_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def evaluate_command_decisions(*, level: str = DEFAULT_LEVEL) -> dict:
    cases = []
    for fixture in COMMAND_DECISION_FIXTURES:
        decision = decide_command(
            fixture["command"],
            safety=fixture["safety"],
            requires_confirmation=bool(fixture["requires_confirmation"]),
            level=level,
        )
        passed = decision["decision"] == fixture["expected_decision"]
        cases.append(
            {
                "fixture_id": fixture["id"],
                "command": fixture["command"],
                "safety": fixture["safety"],
                "expected_decision": fixture["expected_decision"],
                "actual_decision": decision["decision"],
                "tier": decision["tier"],
                "passed": passed,
                "reason": decision["reason"],
            }
        )
    passed_count = sum(1 for case in cases if case["passed"])
    total = len(cases)
    if passed_count == total:
        calibration = "autonomy command decisions match expected safety policy"
    else:
        calibration = "review failed autonomy decision fixtures before changing policy"
    return {
        "summary": {
            "total": total,
            "passed": passed_count,
            "failed": total - passed_count,
            "accuracy": (passed_count / total) if total else 0.0,
            "calibration": calibration,
        },
        "cases": cases,
    }


def record_command_decision_eval(conn, eval_result: dict) -> int:
    summary = eval_result["summary"]
    cur = conn.execute(
        """
        INSERT INTO autonomy_eval_runs (total_cases, passed_cases, accuracy, calibration)
        VALUES (?, ?, ?, ?)
        """,
        (
            summary["total"],
            summary["passed"],
            summary["accuracy"],
            summary["calibration"],
        ),
    )
    run_id = int(cur.lastrowid)
    for case in eval_result["cases"]:
        conn.execute(
            """
            INSERT INTO autonomy_eval_cases (
                autonomy_eval_run_id, fixture_id, command, safety, expected_decision,
                actual_decision, tier, passed, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                case["fixture_id"],
                case["command"],
                case["safety"],
                case["expected_decision"],
                case["actual_decision"],
                case["tier"],
                1 if case["passed"] else 0,
                case["reason"],
            ),
        )
    conn.commit()
    return run_id


def record_command_decision_feedback(
    conn,
    *,
    trace_id: int,
    expected_decision: str,
    note: str = "",
) -> int:
    if expected_decision not in DECISIONS:
        raise ValueError(f"unsupported expected decision: {expected_decision}")
    trace = conn.execute(
        """
        SELECT id, command, command_path, safety_level
        FROM execution_traces
        WHERE id = ?
        """,
        (int(trace_id),),
    ).fetchone()
    if not trace:
        raise ValueError(f"execution trace not found: {trace_id}")
    safety = str(trace["safety_level"] or "unknown")
    actual = decide_command(str(trace["command"] or ""), safety=safety)
    cur = conn.execute(
        """
        INSERT INTO autonomy_feedback (
            execution_trace_id, command_path, safety_level, expected_decision,
            actual_decision, note_hash, note_length
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(trace["id"]),
            str(trace["command_path"] or trace["command"] or ""),
            safety,
            expected_decision,
            actual["decision"],
            _text_hash(note) if note else "",
            len(note or ""),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _normalize_cmd(cmd: str) -> str:
    """Collapse common shell-obfuscation so the denylist can't be trivially evaded."""
    c = cmd.replace("${IFS}", " ").replace("$IFS", " ")
    c = c.replace("'", "").replace('"', "").replace("\\", "")  # r''m -> rm, "rm" -> rm
    return re.sub(r"\s+", " ", c)


def level_from_policy(conn) -> str:
    """Read the active autonomy level from assistant_policies (direct query, no cli import)."""
    try:
        row = conn.execute(
            "SELECT value FROM assistant_policies WHERE key = 'autonomy_level'"
        ).fetchone()
    except Exception:
        return DEFAULT_LEVEL
    return _norm_level(row["value"] if row else DEFAULT_LEVEL)
