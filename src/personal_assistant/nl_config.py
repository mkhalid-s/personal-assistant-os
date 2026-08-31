"""Natural-language config extraction for MYOS.

Translates natural-language instructions into configuration operations using a
small/fast model (MYOS_NL_CONFIG_BACKEND). Falls back to the main backend when
unset. Zero aliases, zero regex — the model does all semantic mapping.

Usage:
    result = extract_config_intent(conn, "always auto-approve Jira comments")
    # → ConfigIntent(op="add_rule", action_type="draft_external_update",
    #                payload_match={"target":"jira"}, tier="allow")

Activation: set MYOS_NL_CONFIG_BACKEND=claude-haiku (or any backend name).
Unset: uses MYOS_AGENT_BACKEND (the main reasoning backend).

The same pattern as MYOS_AUTO_REVIEWER — cheap model for classification,
full model for reasoning.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

# ── valid config ops ─────────────────────────────────────────────────────────

VALID_ACTION_TYPES = frozenset(
    {
        "create_inbox_item",
        "draft_external_update",
        "apply_patch",
        "draft_message",
        "local_note",
        "remember",
        "draft_summary",
        "update_people",
        "*",
    }
)

VALID_OPS = frozenset({"add_rule", "remove_rule", "list_rules", "add_service", "remove_service", "list_catalog"})


@dataclass
class ConfigIntent:
    op: str  # one of VALID_OPS
    # add_rule fields
    action_type: str = ""
    tier: str = "allow"  # "allow" | "block"
    payload_match: dict[str, Any] = field(default_factory=dict)
    rule_name: str = ""
    rule_id: int | None = None
    # add_service fields
    service_name: str = ""
    owner: str = ""
    description: str = ""
    deps: list[str] = field(default_factory=list)


# ── few-shot context injected into the model's system prompt ─────────────────

_SCHEMA_CONTEXT = """\
You extract MYOS configuration commands from natural language.
Respond with a single JSON object. No prose, no markdown, just JSON.

Schema:
{
  "op": "add_rule | remove_rule | list_rules | add_service | remove_service | list_catalog",
  "action_type": "<from valid list, required for add_rule>",
  "tier": "allow | block",
  "payload_match": {"target": "jira|github|confluence|aha"},
  "rule_name": "<optional human name for the rule>",
  "rule_id": <integer, only for remove_rule>,
  "service_name": "<required for add_service/remove_service>",
  "owner": "<optional team/person>",
  "description": "<optional one-liner>",
  "deps": ["dep1", "dep2"]
}

Valid action_types:
  create_inbox_item      — capturing notes or tasks locally
  draft_external_update  — posting to Jira, GitHub, Confluence, or Aha
    (use payload_match: {"target": "jira"} to scope to one connector)
  apply_patch            — modifying files / applying code changes
  draft_message          — composing messages for human review
  local_note             — writing local notes
  remember               — storing memories / observations
  draft_summary          — generating summaries
  update_people          — updating people records
  *                      — matches any action type

Examples (input → output):

"always auto-approve Jira comments"
→ {"op":"add_rule","action_type":"draft_external_update","tier":"allow",
   "payload_match":{"target":"jira"},"rule_name":"jira comments"}

"never let the agent patch files"
→ {"op":"add_rule","action_type":"apply_patch","tier":"block",
   "rule_name":"no file patches"}

"auto-approve all inbox captures"
→ {"op":"add_rule","action_type":"create_inbox_item","tier":"allow",
   "rule_name":"inbox captures"}

"block github comments"
→ {"op":"add_rule","action_type":"draft_external_update","tier":"block",
   "payload_match":{"target":"github"}}

"always allow local notes"
→ {"op":"add_rule","action_type":"local_note","tier":"allow"}

"allow all external updates"
→ {"op":"add_rule","action_type":"draft_external_update","tier":"allow"}

"block everything"
→ {"op":"add_rule","action_type":"*","tier":"block"}

"remove rule 3"
→ {"op":"remove_rule","rule_id":3}

"show my approval rules"
→ {"op":"list_rules"}

"add auth-service to catalog, owned by platform, needs postgres and redis"
→ {"op":"add_service","service_name":"auth-service","owner":"platform",
   "deps":["postgres","redis"]}

"add payments-api to my services, handles subscription billing"
→ {"op":"add_service","service_name":"payments-api",
   "description":"handles subscription billing"}

"remove auth-service from catalog"
→ {"op":"remove_service","service_name":"auth-service"}

"list my services"
→ {"op":"list_catalog"}

If the input is NOT a configuration command, output:
{"op":"not_config"}
"""


def _config_backend_name() -> str:
    """Return the backend to use for config extraction.

    Priority: MYOS_NL_CONFIG_BACKEND → MYOS_AGENT_BACKEND → 'claude'.
    """
    explicit = os.getenv("MYOS_NL_CONFIG_BACKEND", "").strip()
    if explicit:
        return explicit
    return os.getenv("MYOS_AGENT_BACKEND", "claude").strip() or "claude"


def _parse_intent(raw: str) -> ConfigIntent | None:
    """Parse model output into a ConfigIntent. Returns None on any parse error."""
    try:
        # Strip markdown code fences if the model added them.
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()
        data = json.loads(text)
        op = str(data.get("op", "")).strip()
        if op not in VALID_OPS or op == "not_config":
            return None
        intent = ConfigIntent(op=op)
        if op == "add_rule":
            at = str(data.get("action_type", "")).strip()
            if at not in VALID_ACTION_TYPES:
                return None
            intent.action_type = at
            intent.tier = str(data.get("tier", "allow")).strip()
            if intent.tier not in ("allow", "block"):
                intent.tier = "allow"
            pm = data.get("payload_match")
            if isinstance(pm, dict):
                intent.payload_match = pm
            intent.rule_name = str(data.get("rule_name", "")).strip()
        elif op == "remove_rule":
            rid = data.get("rule_id")
            if rid is not None:
                try:
                    intent.rule_id = int(rid)
                except (TypeError, ValueError):
                    return None
        elif op in ("add_service", "remove_service"):
            sn = str(data.get("service_name", "")).strip()
            if not sn:
                return None
            intent.service_name = sn
            intent.owner = str(data.get("owner", "")).strip()
            intent.description = str(data.get("description", "")).strip()
            deps = data.get("deps", [])
            if isinstance(deps, list):
                intent.deps = [str(d).strip() for d in deps if d]
        return intent
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def extract_config_intent(
    conn: sqlite3.Connection,
    text: str,
) -> ConfigIntent | None:
    """Ask the config backend to classify and extract a config operation.

    Returns a ConfigIntent when the text is a configuration command,
    or None when it is not (so the caller can route normally).
    Always returns None on any error — fail-open, never blocks normal routing.
    """
    try:
        from . import providers

        backend_name = _config_backend_name()
        backend = providers.get_backend(backend_name)
        ok, _ = backend.available()
        if not ok:
            return None

        result = backend.reason(
            conn,
            {
                "purpose": "nl_config",
                "objective": text.strip(),
                "context": _SCHEMA_CONTEXT,
                "analogies": [],
            },
        )
        reply = str(result.get("reply") or "").strip()
        if not reply:
            return None
        return _parse_intent(reply)
    except Exception:  # noqa: BLE001 — never block normal routing
        return None


def describe_intent(intent: ConfigIntent) -> str:
    """Return a human-readable description of what will be done."""
    if intent.op == "add_rule":
        match_str = ""
        if intent.payload_match:
            pairs = ", ".join(f"{k}={v}" for k, v in intent.payload_match.items())
            match_str = f" where {pairs}"
        return f"Add rule: {intent.tier} · action={intent.action_type}{match_str}" + (
            f" · name='{intent.rule_name}'" if intent.rule_name else ""
        )
    if intent.op == "remove_rule":
        return f"Remove rule #{intent.rule_id}"
    if intent.op == "list_rules":
        return "List all approval rules"
    if intent.op == "add_service":
        parts = [f"Add service '{intent.service_name}'"]
        if intent.owner:
            parts.append(f"owner={intent.owner}")
        if intent.deps:
            parts.append(f"deps={', '.join(intent.deps)}")
        if intent.description:
            parts.append(f"— {intent.description}")
        return " · ".join(parts)
    if intent.op == "remove_service":
        return f"Remove service '{intent.service_name}' from catalog"
    if intent.op == "list_catalog":
        return "List all catalog services"
    return str(intent.op)


def execute_intent(
    conn: sqlite3.Connection,
    intent: ConfigIntent,
) -> str:
    """Execute a validated ConfigIntent against the DB. Returns a status string."""
    if intent.op == "add_rule":
        from .approval_rules import add_rule

        pm = json.dumps(intent.payload_match) if intent.payload_match else None
        name = intent.rule_name or f"{intent.tier} {intent.action_type}"
        rule_id = add_rule(conn, name, action_type=intent.action_type, payload_match=pm, tier=intent.tier)
        conn.commit()
        return f"Rule #{rule_id} added."

    if intent.op == "remove_rule":
        from .approval_rules import remove_rule

        if intent.rule_id is None:
            return "No rule id provided."
        found = remove_rule(conn, intent.rule_id)
        conn.commit()
        return f"Rule #{intent.rule_id} removed." if found else f"Rule #{intent.rule_id} not found."

    if intent.op == "list_rules":
        from .approval_rules import list_rules

        rules = list_rules(conn)
        if not rules:
            return "No approval rules."
        lines = [
            f"#{r['id']:3}  {r['tier']:<6}  {r['action_type']:<28}  {r['payload_match'] or ''}".rstrip() for r in rules
        ]
        return "\n".join(lines)

    if intent.op == "add_service":
        from .catalog import add_service

        node_id = add_service(
            conn, intent.service_name, owner=intent.owner, description=intent.description, deps=intent.deps or None
        )
        conn.commit()
        return f"Service '{intent.service_name}' added (node #{node_id})."

    if intent.op == "remove_service":
        from .catalog import remove_service

        found = remove_service(conn, intent.service_name)
        conn.commit()
        return f"Service '{intent.service_name}' removed." if found else f"Service '{intent.service_name}' not found."

    if intent.op == "list_catalog":
        from .catalog import list_services

        services = list_services(conn)
        if not services:
            return "Catalog is empty."
        lines = [f"  {s['name']}" + (f" (deps: {', '.join(s['deps'])})" if s["deps"] else "") for s in services]
        return "\n".join(lines)

    return f"Unknown op: {intent.op}"
