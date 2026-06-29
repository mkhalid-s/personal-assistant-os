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

import re

AUTO = "auto"
CONFIRM = "confirm"
BLOCKED = "blocked"
LEVELS = ("safe", "balanced", "bold")
DEFAULT_LEVEL = "balanced"

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
