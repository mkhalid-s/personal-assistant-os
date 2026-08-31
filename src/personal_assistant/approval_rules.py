"""Graduated approval ladder — standing allow/block rules for agent actions.

Inspired by openworker's four-tier approval ladder. Rules are checked in
_handle_proposals() before the interactive approval prompt is shown, so
trusted action types can auto-execute without friction.

Tiers:
  allow — auto-approve actions matching this rule (no prompt)
  block — always deny actions matching this rule (even in auto mode)

Rules are matched in specificity order: payload-match rules (more specific)
fire before type-only rules. Within the same specificity, newer rules win.

Usage:
    add_rule(conn, "inbox items", action_type="create_inbox_item")
    add_rule(conn, "jira comments", action_type="draft_external_update",
             payload_match='{"target": "jira"}')
    check_rule(conn, "create_inbox_item", "{}") -> "allow"
"""

from __future__ import annotations

import json
import sqlite3


def _payload_subset(rule_match: str, payload_json: str) -> bool:
    """Return True when every key-value pair in rule_match appears in payload."""
    try:
        required = json.loads(rule_match)
        actual = json.loads(payload_json or "{}")
        if not isinstance(required, dict) or not isinstance(actual, dict):
            return False
        return all(actual.get(k) == v for k, v in required.items())
    except (TypeError, ValueError):
        return False


def check_rule(
    conn: sqlite3.Connection,
    action_type: str,
    payload_json: str,
) -> str | None:
    """Return 'allow', 'block', or None (no matching rule — ask the user).

    Payload-match rules are evaluated before type-only rules (more specific
    wins). Within the same specificity, the newest rule (highest id) wins.
    The wildcard action_type '*' matches any action type.
    """
    rows = conn.execute(
        """
        SELECT tier, payload_match
        FROM approval_rules
        WHERE action_type = ? OR action_type = '*'
        ORDER BY
            CASE WHEN payload_match IS NOT NULL THEN 0 ELSE 1 END ASC,
            id DESC
        """,
        (action_type,),
    ).fetchall()
    for row in rows:
        match = row["payload_match"]
        if match:
            if _payload_subset(match, payload_json):
                return str(row["tier"])
        else:
            return str(row["tier"])
    return None


def add_rule(
    conn: sqlite3.Connection,
    name: str,
    *,
    action_type: str,
    payload_match: str | None = None,
    tier: str = "allow",
) -> int:
    """Add a standing approval rule. Returns the new rule id.

    action_type: exact action type string or '*' for any.
    payload_match: optional JSON object; rule fires only when the action's
        payload is a superset of this object.
    tier: 'allow' (auto-approve) or 'block' (always deny).
    """
    if tier not in ("allow", "block"):
        raise ValueError(f"tier must be 'allow' or 'block', got {tier!r}")
    cur = conn.execute(
        """
        INSERT INTO approval_rules (name, action_type, payload_match, tier)
        VALUES (?, ?, ?, ?)
        """,
        (name.strip(), action_type.strip(), payload_match, tier),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def remove_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    """Remove a rule by id. Returns True if a row was deleted."""
    rows = conn.execute(
        "DELETE FROM approval_rules WHERE id = ?", (int(rule_id),)
    ).rowcount
    return rows > 0


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    """Return all approval rules ordered by action_type then id."""
    rows = conn.execute(
        """
        SELECT id, name, action_type, payload_match, tier, created_at
        FROM approval_rules
        ORDER BY action_type ASC, id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]
