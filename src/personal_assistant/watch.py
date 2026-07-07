"""P4: project-risk watchers + proactive nudges.

Scans already-synced ``external_items`` (Jira/GitHub/Confluence/Aha) and local
``work_items`` for things that need attention — overdue work, high-risk items,
open PRs awaiting review, high-priority/blocked issues — and turns them into
*proposed* nudges in the approval queue. Sending is always gated by graded
autonomy (a nudge is a ``draft_external_update`` → confirm tier), so the loop can
surface and draft, but a human (or the `bold` level) decides what actually goes out.

Deterministic heuristics here need no network. The Claude brain produces sharper
findings + nudge wording when it has MCP access, by calling ``scan_risks`` and then
the ``propose_*`` tools.
"""

from __future__ import annotations

from datetime import date

from . import agentcore

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_HOT_PRIORITY = ("p0", "p1", "sev1", "critical", "high", "highest", "blocker")
_BLOCKED_STATUS = ("blocked", "impeded", "on hold", "stalled")


def _finding(kind, severity, source, ref, title, reason, owner=None, url=None, connector=None, external_id=None):
    return {
        "kind": kind,
        "severity": severity,
        "source": source,
        "ref": ref,
        "title": title,
        "reason": reason,
        "owner": owner,
        "url": url,
        "connector": connector,
        "external_id": external_id,
        "suggested_nudge": _nudge(kind, title, reason, owner),
    }


def _nudge(kind: str, title: str, reason: str, owner: str | None) -> str:
    who = owner or "the owner"
    title = (title or "this item").strip()
    if kind == "overdue":
        return f"Hi {who} — '{title}' is past its due date ({reason}). Could you share an updated ETA or flag what's blocking it?"
    if kind == "at_risk":
        return f"Flagging '{title}' as at-risk ({reason}). {who}, do you need help unblocking, or should we renegotiate scope/timeline?"
    if kind == "pr_review":
        return f"'{title}' has an open PR awaiting review — can a reviewer take a look so {who} isn't blocked?"
    if kind == "priority_issue":
        return f"'{title}' is high-priority ({reason}). {who}, what's the current status and is anything in the way?"
    return f"Following up on '{title}' ({reason})."


def scan_project_risks(conn, *, risk_threshold: int = 60, limit: int = 25) -> list[dict]:
    today = date.today().isoformat()
    findings: list[dict] = []

    for r in conn.execute(
        "SELECT id, title, due_date, owner FROM work_items "
        "WHERE status='open' AND due_date IS NOT NULL AND due_date != '' AND due_date < ? "
        "ORDER BY due_date ASC LIMIT ?",
        (today, limit),
    ).fetchall():
        findings.append(
            _finding("overdue", "high", "work_item", r["id"], r["title"], f"due {r['due_date']}, past due", r["owner"])
        )

    for r in conn.execute(
        "SELECT id, title, risk_score, owner FROM work_items "
        "WHERE status='open' AND risk_score >= ? ORDER BY risk_score DESC LIMIT ?",
        (risk_threshold, limit),
    ).fetchall():
        sev = "high" if r["risk_score"] >= 80 else "medium"
        findings.append(
            _finding("at_risk", sev, "work_item", r["id"], r["title"], f"risk score {r['risk_score']}", r["owner"])
        )

    for r in conn.execute(
        "SELECT id, connector, external_id, title, status, owner, url FROM external_items "
        "WHERE item_type='pull_request' AND (status IS NULL OR LOWER(status) NOT IN ('merged','closed','done')) "
        "ORDER BY fetched_at DESC LIMIT ?",
        (limit,),
    ).fetchall():
        findings.append(
            _finding(
                "pr_review",
                "medium",
                "external_item",
                r["id"],
                r["title"],
                f"open PR on {r['connector']} awaiting review",
                r["owner"],
                url=r["url"],
                connector=r["connector"],
                external_id=r["external_id"],
            )
        )

    for r in conn.execute(
        "SELECT id, connector, external_id, title, status, priority, owner, url FROM external_items "
        "WHERE item_type IN ('issue', 'feature') ORDER BY fetched_at DESC LIMIT ?",
        (limit,),
    ).fetchall():
        pr = (r["priority"] or "").lower()
        st = (r["status"] or "").lower()
        if pr in _HOT_PRIORITY or any(b in st for b in _BLOCKED_STATUS):
            findings.append(
                _finding(
                    "priority_issue",
                    "high",
                    "external_item",
                    r["id"],
                    r["title"],
                    f"{r['priority'] or r['status']} on {r['connector']}",
                    r["owner"],
                    url=r["url"],
                    connector=r["connector"],
                    external_id=r["external_id"],
                )
            )

    # Dedup by (source, ref), keeping the highest severity; then severity-sort.
    dedup: dict[tuple, dict] = {}
    for f in findings:
        key = (f["source"], f["ref"])
        if key not in dedup or _SEVERITY_ORDER[f["severity"]] < _SEVERITY_ORDER[dedup[key]["severity"]]:
            dedup[key] = f
    return sorted(dedup.values(), key=lambda f: _SEVERITY_ORDER[f["severity"]])[:limit]


def draft_nudges(conn, findings: list[dict], *, limit: int = 10) -> list[int]:
    """Enqueue a confirm-tier nudge proposal per finding. Nothing is sent here."""
    if not findings:
        return []
    task_id = agentcore.ensure_turn_task(conn, "proactive risk nudges")
    ids = []
    for f in findings[:limit]:
        # Route to the item's actual connector (jira/github/confluence/aha), not
        # always Jira (finding #7); local work-item nudges go to the outbox.
        target = (f.get("connector") or "outbox") if f["source"] == "external_item" else "outbox"
        payload = {
            "target": target,
            "draft": f["suggested_nudge"],
            "summary": f["reason"],
            "kind": f["kind"],
            "connector": f.get("connector"),
            "external_id": f.get("external_id"),
            "work_item_id": f["ref"] if f["source"] == "work_item" else None,
        }
        if f.get("url"):
            payload["url"] = f["url"]
        ids.append(
            agentcore.enqueue_proposal(
                conn,
                task_id=task_id,
                action_type="draft_external_update",
                title=f"Nudge: {f['title'][:80]}",
                payload=payload,
                requires_approval=1,
            )
        )
    conn.commit()
    return ids


def risk_signals(conn, *, risk_threshold: int = 60, limit: int = 15) -> list[dict]:
    """Risk findings shaped as autopilot signals (stable keys → no per-cycle dup)."""
    out = []
    for f in scan_project_risks(conn, risk_threshold=risk_threshold, limit=limit):
        out.append(
            {
                "key": f"risk:{f['source']}:{f['ref']}:{f['kind']}",
                "type": "project_risk",
                "source_type": f["source"],
                "source_id": f["ref"],
                "title": f["title"],
                "detail": f"{f['reason']} — suggested nudge: {f['suggested_nudge']}",
                "priority": 1 if f["severity"] == "high" else 2,
            }
        )
    return out
