from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .data_dirs import resolve_data_dir
from .tui_utils import condense_payload, format_age, parse_payload, truncate

INTENTS_LIMIT = 20
APPROVALS_LIMIT = 20
AUDIT_PAGE_SIZE = 25
AUDIT_PAGE_SIZE_MAX = 100
GRAPH_NODE_LIMIT = 200
GRAPH_EDGE_LIMIT = 400
PAYLOAD_PREVIEW_CHARS = 160
GRAPH_JSON_SCHEMA = "myos.dashboard.graph.v1"


def _query_rows(conn: sqlite3.Connection, query: str, params: tuple = ()):
    return conn.execute(query, params).fetchall()


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _query_intents(conn: sqlite3.Connection, *, limit: int = INTENTS_LIMIT) -> list[dict[str, Any]]:
    """Open/active intents with linked open-risk and evidence counts."""
    capped = _clamp_int(limit, default=INTENTS_LIMIT, minimum=1, maximum=100)
    rows = conn.execute(
        """
        SELECT
          i.id,
          i.objective,
          i.status,
          i.priority,
          i.created_at,
          i.updated_at,
          (SELECT COUNT(*) FROM intent_risks r
             WHERE r.intent_id = i.id AND r.status = 'open') AS open_risks,
          (SELECT COUNT(*) FROM intent_evidence e
             WHERE e.intent_id = i.id) AS evidence_count
        FROM intents i
        WHERE i.status IN ('open', 'active')
        ORDER BY i.priority ASC, i.updated_at DESC, i.id DESC
        LIMIT ?
        """,
        (capped,),
    ).fetchall()
    return [dict(r) for r in rows]


def parse_audit_query(query: dict[str, list[str]] | None) -> tuple[int, str, int]:
    """Parse GET query params into (page, event_type, page_size)."""
    query = query or {}
    page = _clamp_int((query.get("page") or ["1"])[0], default=1, minimum=1, maximum=100_000)
    raw_type = (query.get("event_type") or [""])[0]
    event_type = str(raw_type or "").strip()[:80]
    page_size = _clamp_int(
        (query.get("page_size") or [str(AUDIT_PAGE_SIZE)])[0],
        default=AUDIT_PAGE_SIZE,
        minimum=1,
        maximum=AUDIT_PAGE_SIZE_MAX,
    )
    return page, event_type, page_size


def _query_audit_event_types(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    capped = _clamp_int(limit, default=20, minimum=1, maximum=50)
    rows = conn.execute(
        """
        SELECT event_type, COUNT(*) AS n
        FROM event_log
        GROUP BY event_type
        ORDER BY n DESC, event_type ASC
        LIMIT ?
        """,
        (capped,),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_audit_trail(
    conn: sqlite3.Connection,
    *,
    event_type: str = "",
    page: int = 1,
    page_size: int = AUDIT_PAGE_SIZE,
) -> dict[str, Any]:
    """Paged event_log listing. Always bounded by page_size."""
    page = _clamp_int(page, default=1, minimum=1, maximum=100_000)
    page_size = _clamp_int(page_size, default=AUDIT_PAGE_SIZE, minimum=1, maximum=AUDIT_PAGE_SIZE_MAX)
    event_type = str(event_type or "").strip()[:80]
    if event_type:
        total_row = conn.execute(
            "SELECT COUNT(*) AS n FROM event_log WHERE event_type = ?",
            (event_type,),
        ).fetchone()
    else:
        total_row = conn.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()
    total = int(total_row["n"] or 0) if total_row else 0
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = min(page, pages)
    offset = (page - 1) * page_size
    if event_type:
        rows = conn.execute(
            """
            SELECT id, event_type, entity_type, entity_id, payload, created_at
            FROM event_log
            WHERE event_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (event_type, page_size, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, event_type, entity_type, entity_id, payload, created_at
            FROM event_log
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()
    return {
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "event_type": event_type,
    }


def _query_approvals(conn: sqlite3.Connection, *, limit: int = APPROVALS_LIMIT) -> list[dict[str, Any]]:
    """Read-only approval queue: requires_approval and proposed/approved."""
    capped = _clamp_int(limit, default=APPROVALS_LIMIT, minimum=1, maximum=100)
    rows = conn.execute(
        """
        SELECT id, action_type, title, status, payload_json, created_at
        FROM agent_actions
        WHERE requires_approval = 1 AND status IN ('proposed', 'approved')
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (capped,),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_graph_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Node/edge totals and top node types (bounded)."""
    node_row = conn.execute("SELECT COUNT(*) AS n FROM knowledge_nodes").fetchone()
    edge_row = conn.execute("SELECT COUNT(*) AS n FROM knowledge_edges").fetchone()
    type_rows = conn.execute(
        """
        SELECT node_type, COUNT(*) AS n
        FROM knowledge_nodes
        GROUP BY node_type
        ORDER BY n DESC, node_type ASC
        LIMIT 8
        """
    ).fetchall()
    return {
        "node_count": int(node_row["n"] or 0) if node_row else 0,
        "edge_count": int(edge_row["n"] or 0) if edge_row else 0,
        "top_types": [dict(r) for r in type_rows],
    }


def _query_graph_export(
    conn: sqlite3.Connection,
    *,
    node_limit: int = GRAPH_NODE_LIMIT,
    edge_limit: int = GRAPH_EDGE_LIMIT,
) -> dict[str, Any]:
    """Bounded knowledge graph snapshot for JSON export."""
    node_limit = _clamp_int(node_limit, default=GRAPH_NODE_LIMIT, minimum=1, maximum=GRAPH_NODE_LIMIT)
    edge_limit = _clamp_int(edge_limit, default=GRAPH_EDGE_LIMIT, minimum=1, maximum=GRAPH_EDGE_LIMIT)
    summary = _query_graph_summary(conn)
    nodes = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, node_type, ref_id, label
            FROM knowledge_nodes
            ORDER BY id DESC
            LIMIT ?
            """,
            (node_limit,),
        ).fetchall()
    ]
    node_ids = [int(n["id"]) for n in nodes]
    if node_ids:
        placeholders = ",".join("?" * len(node_ids))
        edges = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT id, from_node_id, to_node_id, relation, weight, source
                FROM knowledge_edges
                WHERE from_node_id IN ({placeholders}) AND to_node_id IN ({placeholders})
                ORDER BY id DESC
                LIMIT ?
                """,
                (*node_ids, *node_ids, edge_limit),
            ).fetchall()
        ]
    else:
        edges = []
    truncated = summary["node_count"] > len(nodes) or summary["edge_count"] > len(edges)
    return {
        "schema": GRAPH_JSON_SCHEMA,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated,
        "counts": summary,
        "limits": {"nodes": node_limit, "edges": edge_limit},
    }


def export_graph_json(conn: sqlite3.Connection) -> str:
    return json.dumps(_query_graph_export(conn), ensure_ascii=True, indent=2)


def _payload_preview(payload_json: str | None) -> str:
    payload = parse_payload(str(payload_json or "{}"))
    preview = condense_payload(payload, PAYLOAD_PREVIEW_CHARS) if payload else ""
    if preview:
        return preview
    raw = str(payload_json or "").strip()
    if not raw or raw == "{}":
        return ""
    return truncate(raw, PAYLOAD_PREVIEW_CHARS)


def _audit_query_href(*, token: str, page: int, event_type: str) -> str:
    params: dict[str, str] = {}
    if token:
        params["token"] = token
    if page > 1:
        params["page"] = str(page)
    if event_type:
        params["event_type"] = event_type
    query = urlencode(params)
    return f"?{query}" if query else "?"


def render_dashboard_html(
    conn: sqlite3.Connection,
    report_dir: str = "",
    *,
    audit_page: int = 1,
    audit_event_type: str = "",
    audit_page_size: int = AUDIT_PAGE_SIZE,
    request_token: str = "",
) -> str:
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_items,
          (SELECT COUNT(*) FROM work_items WHERE status='done') AS done_items,
          (SELECT COUNT(*) FROM work_items WHERE status='open' AND risk_score >= 60) AS at_risk,
          (SELECT COUNT(*) FROM inbox_items WHERE status='new') AS inbox_new
        """
    ).fetchone()

    risk_rows = _query_rows(
        conn,
        """
        SELECT id, title, risk_score, due_date
        FROM work_items
        WHERE status='open' AND risk_score >= 60
        ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
        LIMIT 10
        """,
    )
    evidence_rows = _query_rows(
        conn,
        """
        SELECT person, category, impact, created_at
        FROM review_evidence
        ORDER BY created_at DESC
        LIMIT 10
        """,
    )
    trend_7 = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM event_log WHERE created_at >= datetime('now', '-7 days')) AS events,
          (SELECT COUNT(*) FROM event_log WHERE event_type IN ('stop_doing_review', 'renegotiate_review')
             AND created_at >= datetime('now', '-7 days')) AS interventions,
          (SELECT SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END)
             FROM commitment_log
             WHERE resolved_on IS NOT NULL AND resolved_on >= date('now', '-7 days')) AS on_time,
          (SELECT SUM(CASE WHEN outcome IN ('completed_on_time', 'completed_late', 'missed') THEN 1 ELSE 0 END)
             FROM commitment_log
             WHERE resolved_on IS NOT NULL AND resolved_on >= date('now', '-7 days')) AS resolved_total
        """
    ).fetchone()
    trend_30 = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM event_log WHERE created_at >= datetime('now', '-30 days')) AS events,
          (SELECT COUNT(*) FROM event_log WHERE event_type IN ('stop_doing_review', 'renegotiate_review')
             AND created_at >= datetime('now', '-30 days')) AS interventions,
          (SELECT SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END)
             FROM commitment_log
             WHERE resolved_on IS NOT NULL AND resolved_on >= date('now', '-30 days')) AS on_time,
          (SELECT SUM(CASE WHEN outcome IN ('completed_on_time', 'completed_late', 'missed') THEN 1 ELSE 0 END)
             FROM commitment_log
             WHERE resolved_on IS NOT NULL AND resolved_on >= date('now', '-30 days')) AS resolved_total
        """
    ).fetchone()

    intent_rows = _query_intents(conn)
    audit = _query_audit_trail(conn, event_type=audit_event_type, page=audit_page, page_size=audit_page_size)
    audit_types = _query_audit_event_types(conn)
    approval_rows = _query_approvals(conn)
    graph_summary = _query_graph_summary(conn)

    report_links = []
    # PAOS-003: default through data_dirs (MYOS_DATA_DIR > dev repo data/ >
    # platform data dir) so installed builds resolve reports outside site-packages.
    rdir = Path(report_dir) if report_dir else resolve_data_dir() / "reports"
    if rdir.exists():
        report_links = sorted(rdir.glob("daily-brief-*.md"), reverse=True)[:10]

    risk_items = (
        "".join(
            f"<li><strong>#{r['id']}</strong> {escape(r['title'])} (risk={r['risk_score']}, due={escape(r['due_date'] or 'none')})</li>"
            for r in risk_rows
        )
        or "<li>No high-risk items.</li>"
    )

    evidence_items = (
        "".join(
            f"<li><strong>{escape(r['person'])}</strong> [{escape(r['category'])}] - {escape(r['impact'])}<br><small>{escape(r['created_at'])}</small></li>"
            for r in evidence_rows
        )
        or "<li>No evidence entries yet.</li>"
    )

    report_items = (
        "".join(f"<li><a href='file://{escape(str(p))}'>{escape(p.name)}</a></li>" for p in report_links)
        or "<li>No reports found.</li>"
    )

    intent_items = (
        "".join(
            (
                "<tr>"
                f"<td>#{row['id']}</td>"
                f"<td>{escape(str(row.get('objective') or ''))}</td>"
                f"<td>{escape(str(row.get('status') or ''))}</td>"
                f"<td>{escape(str(row.get('priority') or ''))}</td>"
                f"<td>{escape(format_age(str(row.get('created_at') or '')) or str(row.get('created_at') or ''))}</td>"
                f"<td>{int(row.get('open_risks') or 0)}</td>"
                f"<td>{int(row.get('evidence_count') or 0)}</td>"
                "</tr>"
            )
            for row in intent_rows
        )
        or "<tr><td colspan='7'>No open or active intents.</td></tr>"
    )

    token_hidden = (
        f"<input type='hidden' name='token' value='{escape(request_token, quote=True)}'>" if request_token else ""
    )
    type_chips = "".join(
        (
            "<a href='"
            f"{escape(_audit_query_href(token=request_token, page=1, event_type=str(row['event_type'])))}'>"
            f"{escape(str(row['event_type']))} ({int(row['n'])})</a> "
        )
        for row in audit_types
    )
    all_href = escape(_audit_query_href(token=request_token, page=1, event_type=""))
    audit_rows_html = (
        "".join(
            (
                "<tr>"
                f"<td>#{row['id']}</td>"
                f"<td>{escape(str(row.get('event_type') or ''))}</td>"
                f"<td>{escape(str(row.get('entity_type') or ''))} "
                f"{escape(str(row.get('entity_id') if row.get('entity_id') is not None else ''))}</td>"
                f"<td>{escape(truncate(str(row.get('payload') or ''), 120))}</td>"
                f"<td>{escape(str(row.get('created_at') or ''))}</td>"
                "</tr>"
            )
            for row in audit["rows"]
        )
        or "<tr><td colspan='5'>No audit events.</td></tr>"
    )
    prev_page = audit["page"] - 1 if audit["page"] > 1 else 1
    next_page = audit["page"] + 1 if audit["page"] < audit["pages"] else audit["pages"]
    prev_href = escape(_audit_query_href(token=request_token, page=prev_page, event_type=str(audit["event_type"])))
    next_href = escape(_audit_query_href(token=request_token, page=next_page, event_type=str(audit["event_type"])))
    pager = (
        f"<div class='muted'>Page {audit['page']} of {audit['pages']} · {audit['total']} events"
        f" · <a href='{prev_href}'>Prev</a> · <a href='{next_href}'>Next</a></div>"
        if request_token or audit["pages"] > 1
        else f"<div class='muted'>Page {audit['page']} of {audit['pages']} · {audit['total']} events</div>"
    )

    approval_items = (
        "".join(
            (
                "<tr>"
                f"<td>#{row['id']}</td>"
                f"<td>{escape(str(row.get('title') or ''))}</td>"
                f"<td>{escape(str(row.get('action_type') or ''))}</td>"
                f"<td>{escape(format_age(str(row.get('created_at') or '')) or str(row.get('created_at') or ''))}</td>"
                f"<td><code>{escape(_payload_preview(row.get('payload_json')))}</code></td>"
                "</tr>"
            )
            for row in approval_rows
        )
        or "<tr><td colspan='5'>No pending approvals.</td></tr>"
    )

    type_summary = (
        ", ".join(f"{escape(str(row['node_type']))} ({int(row['n'])})" for row in graph_summary["top_types"]) or "none"
    )
    graph_export_href = "/graph.json"
    if request_token:
        graph_export_href += f"?token={escape(request_token, quote=True)}"

    def _pct(numerator: int | None, denominator: int | None) -> str:
        num = numerator or 0
        den = denominator or 0
        if den <= 0:
            return "n/a"
        return f"{(100.0 * num / den):.1f}%"

    trend_rows = "".join(
        [
            "<tr><td>7d</td>"
            f"<td>{trend_7['events'] or 0}</td>"
            f"<td>{trend_7['interventions'] or 0}</td>"
            f"<td>{_pct(trend_7['interventions'], trend_7['events'])}</td>"
            f"<td>{_pct(trend_7['on_time'], trend_7['resolved_total'])}</td>"
            "</tr>",
            "<tr><td>30d</td>"
            f"<td>{trend_30['events'] or 0}</td>"
            f"<td>{trend_30['interventions'] or 0}</td>"
            f"<td>{_pct(trend_30['interventions'], trend_30['events'])}</td>"
            f"<td>{_pct(trend_30['on_time'], trend_30['resolved_total'])}</td>"
            "</tr>",
        ]
    )

    generated = datetime.now().isoformat(timespec="minutes")
    return f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <title>MYOS Dashboard</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 12px; margin-bottom: 20px; }}
    .card {{ background: #1e293b; border-radius: 10px; padding: 12px; }}
    h1,h2 {{ margin: 0 0 10px 0; }}
    section {{ background: #111827; border-radius: 10px; padding: 12px; margin-bottom: 12px; }}
    a {{ color: #93c5fd; }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin-bottom: 8px; }}
    .muted {{ color: #94a3b8; font-size: 12px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #334155; text-align: left; padding: 8px; vertical-align: top; }}
    code {{ font-size: 12px; color: #cbd5e1; }}
    form.filter {{ margin: 8px 0; }}
    input[type=text] {{ background: #0f172a; color: #e2e8f0; border: 1px solid #334155; padding: 4px 8px; }}
    button {{ background: #334155; color: #e2e8f0; border: 0; padding: 4px 10px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>Personal Assistant Dashboard</h1>
  <div class='muted'>Generated at {generated}</div>
  <div class='cards'>
    <div class='card'><div>Open Items</div><h2>{counts["open_items"]}</h2></div>
    <div class='card'><div>Done Items</div><h2>{counts["done_items"]}</h2></div>
    <div class='card'><div>At Risk</div><h2>{counts["at_risk"]}</h2></div>
    <div class='card'><div>Inbox New</div><h2>{counts["inbox_new"]}</h2></div>
  </div>
  <section>
    <h2>Risk Watch</h2>
    <ul>{risk_items}</ul>
  </section>
  <section>
    <h2>Review Evidence</h2>
    <ul>{evidence_items}</ul>
  </section>
  <section id='intents'>
    <h2>Intents</h2>
    <table>
      <thead>
        <tr><th>ID</th><th>Objective</th><th>Status</th><th>Priority</th><th>Age</th><th>Open risks</th><th>Evidence</th></tr>
      </thead>
      <tbody>{intent_items}</tbody>
    </table>
  </section>
  <section id='approvals'>
    <h2>Approvals</h2>
    <div class='muted'>Read-only. Approve or deny from the CLI (`myos approve`).</div>
    <table>
      <thead>
        <tr><th>ID</th><th>Title</th><th>Type</th><th>Age</th><th>Payload</th></tr>
      </thead>
      <tbody>{approval_items}</tbody>
    </table>
  </section>
  <section id='audit'>
    <h2>Audit trail</h2>
    <form class='filter' method='get'>
      {token_hidden}
      <label>event_type <input type='text' name='event_type' value='{escape(str(audit["event_type"]))}'></label>
      <button type='submit'>Filter</button>
      <a href='{all_href}'>All types</a>
    </form>
    <div class='muted'>Types: {type_chips or "none"}</div>
    <table>
      <thead>
        <tr><th>ID</th><th>Type</th><th>Entity</th><th>Payload</th><th>When</th></tr>
      </thead>
      <tbody>{audit_rows_html}</tbody>
    </table>
    {pager}
  </section>
  <section id='graph'>
    <h2>Graph context</h2>
    <p>{graph_summary["node_count"]} nodes · {graph_summary["edge_count"]} edges</p>
    <p class='muted'>Top node types: {type_summary}</p>
    <p><a href='{graph_export_href}'>Export bounded graph JSON</a> (GET /graph.json)</p>
  </section>
  <section>
    <h2>Trends (7/30 days)</h2>
    <table>
      <thead>
        <tr><th>Window</th><th>Events</th><th>Interventions</th><th>Intervention Rate</th><th>Acceptance Rate</th></tr>
      </thead>
      <tbody>{trend_rows}</tbody>
    </table>
    <div class='muted'>Acceptance rate = commitments completed_on_time / resolved commitments. Intervention rate = stop-doing + renegotiate reviews / events.</div>
  </section>
  <section>
    <h2>Latest Reports</h2>
    <ul>{report_items}</ul>
  </section>
</body>
</html>
"""


def dashboard_http_response(
    conn: sqlite3.Connection,
    *,
    path: str,
    query: dict[str, list[str]],
    token: str,
    supplied_token: str,
    report_dir: str = "",
) -> tuple[int, str, bytes]:
    """Read-only GET dispatcher. Returns (status, content_type, body)."""
    if not secrets.compare_digest(supplied_token, token):
        return 401, "text/plain; charset=utf-8", b"unauthorized: use the tokened URL printed at startup\n"
    route = path.rstrip("/") or "/"
    if route == "/graph.json":
        return 200, "application/json; charset=utf-8", export_graph_json(conn).encode("utf-8")
    page, event_type, page_size = parse_audit_query(query)
    html = render_dashboard_html(
        conn,
        report_dir=report_dir,
        audit_page=page,
        audit_event_type=event_type,
        audit_page_size=page_size,
        request_token=token,
    )
    return 200, "text/html; charset=utf-8", html.encode("utf-8")


def serve_dashboard(conn: sqlite3.Connection, host: str = "127.0.0.1", port: int = 8787, report_dir: str = "") -> None:
    # PAOS-042: the dashboard renders personal schedule/risk data, and a bare
    # localhost bind does not stop other local processes (or a browser
    # DNS-rebinding page) from reading it. Every serve mints a random token;
    # requests must present it as a query parameter or get a 401.
    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            supplied = (query.get("token") or [""])[0]
            status, content_type, body = dashboard_http_response(
                conn,
                path=parsed.path,
                query=query,
                token=token,
                supplied_token=supplied,
                report_dir=report_dir,
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            # R9/PAOS-042: BaseHTTPRequestHandler's default request logging
            # writes each request line — including the ?token= query string —
            # to stderr. The token is the dashboard's only access control, so
            # request logging is suppressed entirely (a redacted path would
            # still leak which URLs were requested with which shape); the
            # tokened URL is printed exactly once at startup instead.
            return

    server = HTTPServer((host, port), Handler)
    print(f"Serving dashboard at http://{host}:{port}/?token={token}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
