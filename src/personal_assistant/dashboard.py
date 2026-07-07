from __future__ import annotations

from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sqlite3


def _query_rows(conn: sqlite3.Connection, query: str, params: tuple = ()):
    return conn.execute(query, params).fetchall()


def render_dashboard_html(conn: sqlite3.Connection, report_dir: str = "") -> str:
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

    report_links = []
    if report_dir:
        rdir = Path(report_dir)
    else:
        rdir = Path(__file__).resolve().parents[2] / "data" / "reports"
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
    th, td {{ border-bottom: 1px solid #334155; text-align: left; padding: 8px; }}
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


def serve_dashboard(conn: sqlite3.Connection, host: str = "127.0.0.1", port: int = 8787, report_dir: str = "") -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = render_dashboard_html(conn, report_dir=report_dir).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = HTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
