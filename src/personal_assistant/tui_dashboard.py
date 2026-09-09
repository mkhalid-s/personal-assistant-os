"""L3 — Live terminal status dashboard for MYOS.

Displays a self-refreshing operator overview: approval queue, at-risk work
items, recent events, and loop/digest status. Requires the [tui] extra
(rich>=13). Falls back to a plain-text one-shot print when rich is absent.

Usage:
    from .tui_dashboard import live_status, status_plain
    live_status(conn, interval=5)   # blocks until user presses q
    status_plain(conn)              # one-shot, no rich needed
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from .tui_utils import condense_payload, format_age, parse_payload, truncate

# ---------------------------------------------------------------------------
# Data layer — pure functions, no rich/textual, independently testable
# ---------------------------------------------------------------------------


def _query_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Proposed/approved actions that need operator attention."""
    rows = conn.execute(
        """
        SELECT id, action_type, title, status, payload_json, created_at
        FROM agent_actions
        WHERE requires_approval = 1 AND status IN ('proposed', 'approved')
        ORDER BY created_at ASC
        LIMIT 10
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _query_work_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open work items ordered by risk, capped at 6 for display."""
    try:
        rows = conn.execute(
            """
            SELECT id, title, risk_score, due_date
            FROM work_items
            WHERE status = 'open'
            ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
            LIMIT 6
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001 — table may not exist in minimal installs
        return []


def _query_recent_events(conn: sqlite3.Connection, *, limit: int = 8) -> list[dict[str, Any]]:
    """Recent event_log rows for the events/audit panel. Default 8; callers may request more."""
    try:
        capped = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        capped = 8
    try:
        rows = conn.execute(
            """
            SELECT event_type, entity_type, entity_id, payload, created_at
            FROM event_log
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (capped,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _query_intents(conn: sqlite3.Connection, *, limit: int = 8) -> list[dict[str, Any]]:
    """Open/active intents with open-risk and evidence counts."""
    try:
        capped = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        capped = 8
    try:
        rows = conn.execute(
            """
            SELECT
              i.id,
              i.objective,
              i.status,
              i.priority,
              i.created_at,
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
    except Exception:  # noqa: BLE001
        return []


def _query_graph_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Knowledge graph node/edge totals and top node types."""
    empty: dict[str, Any] = {"node_count": 0, "edge_count": 0, "top_types": []}
    try:
        node_row = conn.execute("SELECT COUNT(*) AS n FROM knowledge_nodes").fetchone()
        edge_row = conn.execute("SELECT COUNT(*) AS n FROM knowledge_edges").fetchone()
        type_rows = conn.execute(
            """
            SELECT node_type, COUNT(*) AS n
            FROM knowledge_nodes
            GROUP BY node_type
            ORDER BY n DESC, node_type ASC
            LIMIT 6
            """
        ).fetchall()
        return {
            "node_count": int(node_row["n"] or 0) if node_row else 0,
            "edge_count": int(edge_row["n"] or 0) if edge_row else 0,
            "top_types": [dict(r) for r in type_rows],
        }
    except Exception:  # noqa: BLE001
        return empty


def _query_inbox_count(conn: sqlite3.Connection) -> int:
    """Number of unread inbox items."""
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM inbox_items WHERE status = 'new'").fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _query_latest_digest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Most recent assistant digest title and age."""
    try:
        row = conn.execute(
            "SELECT title, created_at FROM assistant_digests ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


def _query_loop_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Latest autonomy_run_ledger row for the header status line."""
    try:
        row = conn.execute(
            """
            SELECT decision_type, status, pending_approvals, created_at
            FROM autonomy_run_ledger
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else {}
    except Exception:  # noqa: BLE001
        return {}


def _query_work_count(conn: sqlite3.Connection) -> dict[str, int]:
    """Aggregate counts for the work panel header."""
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN risk_score >= 60 THEN 1 ELSE 0 END) AS at_risk
            FROM work_items
            WHERE status = 'open'
            """
        ).fetchone()
        return (
            {"total": int(row["total"] or 0), "at_risk": int(row["at_risk"] or 0)}
            if row
            else {"total": 0, "at_risk": 0}
        )
    except Exception:  # noqa: BLE001
        return {"total": 0, "at_risk": 0}


def build_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Collect all panel data in one shot. Returns a plain dict for easy testing."""
    return {
        "queue": _query_queue(conn),
        "work_items": _query_work_items(conn),
        "work_counts": _query_work_count(conn),
        "events": _query_recent_events(conn, limit=20),
        "intents": _query_intents(conn, limit=8),
        "graph": _query_graph_counts(conn),
        "inbox_new": _query_inbox_count(conn),
        "digest": _query_latest_digest(conn),
        "loop": _query_loop_status(conn),
        "backend_name": os.getenv("MYOS_AGENT_BACKEND", "claude"),
    }


def _due_label(due_date: str | None) -> str:
    """Convert ISO due_date to a display label relative to today."""
    if not due_date:
        return ""
    try:
        from datetime import date

        due = date.fromisoformat(str(due_date)[:10])
        today = date.today()
        delta = (due - today).days
        if delta < 0:
            return f"overdue ({abs(delta)}d)"
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        return f"+{delta}d"
    except (ValueError, TypeError):
        return str(due_date)


def _event_summary(event: dict[str, Any]) -> str:
    """Compact one-line summary of an event_log row."""
    etype = str(event.get("event_type") or "")
    entity_id = event.get("entity_id")
    payload_str = str(event.get("payload") or "")
    extra = ""
    if entity_id is not None:
        extra = f" #{entity_id}"
    if payload_str and payload_str != "{}":
        try:
            import json

            p = json.loads(payload_str)
            if isinstance(p, dict):
                parts = [f"{k}={v}" for k, v in list(p.items())[:3] if v is not None]
                if parts:
                    extra += "  " + "  ".join(parts)
        except (ValueError, TypeError):
            pass
    return truncate(etype + extra, 70)


# ---------------------------------------------------------------------------
# Plain-text fallback — no rich needed
# ---------------------------------------------------------------------------


def status_plain(conn: sqlite3.Connection) -> None:
    """One-shot plain-text status print for non-TTY or no-rich environments."""
    from datetime import datetime

    snapshot = build_snapshot(conn)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    backend = snapshot["backend_name"]
    loop = snapshot["loop"]
    loop_str = f"{loop.get('status', 'idle')} ({format_age(str(loop.get('created_at', '')))} ago)" if loop else "idle"

    print(f"MYOS Status  {now}")
    print(f"Backend: {backend}   Loop: {loop_str}")

    digest = snapshot["digest"]
    if digest:
        print(
            f"Last digest: {truncate(str(digest.get('title', '')), 60)}  ({format_age(str(digest.get('created_at', '')))} ago)"
        )

    queue = snapshot["queue"]
    print(f"\nApproval queue: {len(queue)} pending")
    for row in queue[:5]:
        payload = parse_payload(str(row.get("payload_json", "{}")))
        target = payload.get("target") or payload.get("target_ref") or ""
        target_str = f"  {target}" if target else ""
        print(
            f"  #{row['id']}  {row['action_type']}{target_str}  {row['status']}  {format_age(str(row.get('created_at', '')))}"
        )
        preview = condense_payload(payload, 80)
        if preview:
            print(f"    {preview}")

    intents = snapshot.get("intents") or []
    print(f"\nIntents: {len(intents)} open/active")
    for intent in intents[:5]:
        print(
            f"  #{intent.get('id')}  {truncate(str(intent.get('objective') or ''), 50)}  "
            f"{intent.get('status')}  pri={intent.get('priority')}  "
            f"{format_age(str(intent.get('created_at') or ''))}  "
            f"risks={int(intent.get('open_risks') or 0)}  evidence={int(intent.get('evidence_count') or 0)}"
        )

    wc = snapshot["work_counts"]
    print(f"\nWork: {wc['total']} open  {wc['at_risk']} at-risk")
    for item in snapshot["work_items"][:4]:
        risk = int(item.get("risk_score") or 0)
        flag = "⚠ " if risk >= 60 else "  "
        due = _due_label(str(item.get("due_date") or ""))
        due_str = f"  due {due}" if due else ""
        print(f"  {flag}{item.get('title', '')}  risk={risk}{due_str}")

    inbox_new = snapshot["inbox_new"]
    if inbox_new:
        print(f"\nInbox: {inbox_new} new")

    events = snapshot["events"]
    if events:
        print("\nRecent events:")
        for ev in events[:12]:
            age = format_age(str(ev.get("created_at", "")))
            print(f"  {age:>6}  {_event_summary(ev)}")

    graph = snapshot.get("graph") or {}
    types = graph.get("top_types") or []
    type_str = ", ".join(f"{row.get('node_type')}={int(row.get('n') or 0)}" for row in types[:4])
    type_note = f"  types: {type_str}" if type_str else ""
    print(f"\nGraph: {int(graph.get('node_count') or 0)} nodes  {int(graph.get('edge_count') or 0)} edges{type_note}")

    print("\nRun: pip install 'personal-assistant-os[tui]'  for the live dashboard.")


# ---------------------------------------------------------------------------
# Rich live dashboard
# ---------------------------------------------------------------------------


def _render_layout(snapshot: dict[str, Any]) -> Any:
    """Build a rich Layout from a snapshot dict. Returns the Layout object."""
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    # --- header ---
    backend = snapshot["backend_name"]
    loop = snapshot["loop"]
    if loop:
        loop_status = str(loop.get("status") or "idle")
        loop_age = format_age(str(loop.get("created_at") or ""))
        _STATUS_STYLE = {
            "completed": "green",
            "running": "bold yellow",
            "sleeping": "dim",
            "blocked": "red",
            "failed": "bold red",
        }
        loop_style = _STATUS_STYLE.get(loop_status, "")
        loop_txt = Text()
        loop_txt.append("Loop: ")
        loop_txt.append(loop_status, style=loop_style)
        if loop_age:
            loop_txt.append(f" ({loop_age} ago)", style="dim")
        pending = int(loop.get("pending_approvals") or 0)
        if pending:
            loop_txt.append(f"  ·  {pending} pending approval(s)", style="yellow")
    else:
        loop_txt = Text("Loop: idle", style="dim")

    digest = snapshot["digest"]
    digest_txt = ""
    if digest:
        digest_txt = f"  ·  Digest: {format_age(str(digest.get('created_at', '')))} ago"

    from datetime import datetime

    header_text = Text()
    header_text.append("MYOS  ", style="bold")
    header_text.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), style="dim")
    header_text.append(f"   Backend: {backend}   ")
    header_text.append_text(loop_txt)
    header_text.append(digest_txt, style="dim")
    header_panel = Panel(header_text, style="bold blue", padding=(0, 1))

    # --- queue panel ---
    queue = snapshot["queue"]
    q_table = Table.grid(padding=(0, 1))
    q_table.add_column(width=4)
    q_table.add_column(width=28)
    q_table.add_column(width=10)
    q_table.add_column(width=6)
    _S_STYLE = {"proposed": "yellow", "approved": "green", "expired": "bold red"}
    for i, row in enumerate(queue[:8]):
        cursor = "▶" if i == 0 else " "
        status = str(row.get("status") or "")
        age = format_age(str(row.get("created_at") or ""))
        payload = parse_payload(str(row.get("payload_json") or "{}"))
        preview = condense_payload(payload, 28)
        action_label = truncate(str(row.get("action_type") or ""), 28)
        if preview:
            action_label = truncate(f"{action_label} {preview}", 28)
        q_table.add_row(
            cursor,
            action_label,
            Text(status, style=_S_STYLE.get(status, "")),
            Text(age, style="dim"),
        )
    if not queue:
        q_table.add_row("", Text("No pending approvals", style="dim green"), "", "")
    queue_panel = Panel(
        q_table, title=f"[bold]PENDING ({len(queue)})[/bold]", border_style="yellow" if queue else "green"
    )

    # --- work items panel ---
    wc = snapshot["work_counts"]
    work_table = Table.grid(padding=(0, 1))
    work_table.add_column(width=3)
    work_table.add_column()
    work_table.add_column(width=8)
    work_table.add_column(width=10)
    for item in snapshot["work_items"][:6]:
        risk = int(item.get("risk_score") or 0)
        flag = Text("⚠ ", style="yellow") if risk >= 60 else Text("  ")
        due = _due_label(str(item.get("due_date") or ""))
        due_style = "bold red" if "overdue" in due or due == "today" else "yellow" if due == "tomorrow" else "dim"
        work_table.add_row(
            flag,
            truncate(str(item.get("title") or ""), 36),
            Text(f"risk={risk}", style="red" if risk >= 60 else "dim"),
            Text(due, style=due_style),
        )
    if not snapshot["work_items"]:
        work_table.add_row("", Text("No open work items", style="dim"), "", "")
    work_panel = Panel(
        work_table,
        title=f"[bold]WORK ({wc['total']} open · {wc['at_risk']} at-risk)[/bold]",
        border_style="red" if wc["at_risk"] > 0 else "blue",
    )

    # --- intents panel ---
    intents = snapshot.get("intents") or []
    intent_table = Table.grid(padding=(0, 1))
    intent_table.add_column(width=4)
    intent_table.add_column()
    intent_table.add_column(width=14)
    for intent in intents[:6]:
        intent_table.add_row(
            f"#{intent.get('id')}",
            truncate(str(intent.get("objective") or ""), 32),
            Text(
                f"r={int(intent.get('open_risks') or 0)} e={int(intent.get('evidence_count') or 0)}",
                style="dim",
            ),
        )
    if not intents:
        intent_table.add_row("", Text("No open intents", style="dim"), "")
    intents_panel = Panel(
        intent_table,
        title=f"[bold]INTENTS ({len(intents)})[/bold]",
        border_style="cyan",
    )

    # --- events panel ---
    ev_table = Table.grid(padding=(0, 1))
    ev_table.add_column(width=6, style="dim")
    ev_table.add_column()
    for ev in snapshot["events"][:16]:
        age = format_age(str(ev.get("created_at") or ""))
        ev_table.add_row(age, _event_summary(ev))
    if not snapshot["events"]:
        ev_table.add_row("", Text("No events yet", style="dim"))
    events_panel = Panel(ev_table, title="[bold]AUDIT EVENTS[/bold]", border_style="blue")

    # --- footer ---
    inbox_new = snapshot["inbox_new"]
    inbox_txt = f"Inbox: {inbox_new} new  ·  " if inbox_new else ""
    graph = snapshot.get("graph") or {}
    graph_txt = f"Graph: {int(graph.get('node_count') or 0)}n/{int(graph.get('edge_count') or 0)}e  ·  "
    footer_text = Text(
        f"{inbox_txt}{graph_txt}[q] quit   [r] refresh   myos approve --tui for actions",
        style="dim",
    )
    footer_panel = Panel(footer_text, padding=(0, 1))

    # --- assemble ---
    layout = Layout()
    layout.split_column(
        Layout(header_panel, name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(events_panel, name="events", size=14),
        Layout(footer_panel, name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(queue_panel, name="queue", ratio=2),
        Layout(work_panel, name="work", ratio=3),
        Layout(intents_panel, name="intents", ratio=2),
    )
    return layout


def live_status(conn: sqlite3.Connection, *, interval: int = 5) -> None:
    """Live-refreshing terminal dashboard. Blocks until 'q' or Ctrl-C.

    Requires rich>=13 ([tui] extra). Falls back to status_plain() if not
    installed. The stdin watcher thread only reads characters and never
    touches the sqlite connection, so check_same_thread safety is preserved.
    """
    try:
        import select
        import sys
        import threading

        from rich.console import Console
        from rich.live import Live
    except ImportError:
        print("Live dashboard requires: pip install 'personal-assistant-os[tui]'")
        print()
        status_plain(conn)
        return

    console = Console()
    quit_event = threading.Event()

    def _input_watcher() -> None:
        try:
            if not sys.stdin.isatty():
                return  # no keyboard input in non-TTY; quit via Ctrl-C only
            while not quit_event.is_set():
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch in ("q", "Q"):
                            quit_event.set()
                except OSError:
                    break
        except Exception:  # noqa: BLE001
            pass

    watcher = threading.Thread(target=_input_watcher, daemon=True)
    watcher.start()

    try:
        with Live(console=console, refresh_per_second=4, screen=True) as live:
            while not quit_event.is_set():
                try:
                    snapshot = build_snapshot(conn)
                    live.update(_render_layout(snapshot))
                except Exception:  # noqa: BLE001 — never crash the live loop
                    pass
                quit_event.wait(interval)
    except KeyboardInterrupt:
        pass
    finally:
        quit_event.set()
