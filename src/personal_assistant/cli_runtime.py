from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from . import cli_health
from .dashboard import export_graph_json, render_dashboard_html, serve_dashboard
from .data_dirs import resolve_data_dir
from .db import connection, get_connection


def cmd_launchd_status(_: argparse.Namespace) -> None:
    labels = ["com.myos.sync", "com.myos.pulse", "com.myos.autopilot"]
    print("Launchd status:")
    launchctl = shutil.which("launchctl")
    if not launchctl:
        for label in labels:
            print(f"- {label}: unavailable (launchctl not found)")
        return
    for label in labels:
        proc = subprocess.run(
            [launchctl, "list", label],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            print(f"- {label}: loaded")
        else:
            print(f"- {label}: not_loaded")


def cmd_dashboard(args: argparse.Namespace) -> None:
    if args.once:
        output_path = (
            Path(args.output_html)
            if args.output_html
            else (resolve_data_dir() / "dashboard.html")
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        graph_out = str(getattr(args, "output_graph_json", "") or "").strip()
        with connection() as conn:
            output_path.write_text(render_dashboard_html(conn, report_dir=args.report_dir))
            print(f"Dashboard snapshot written: {output_path}")
            if graph_out:
                graph_path = Path(graph_out)
                graph_path.parent.mkdir(parents=True, exist_ok=True)
                graph_path.write_text(export_graph_json(conn))
                print(f"Graph snapshot written: {graph_path}")
        return
    conn = get_connection()
    print(f"Serving dashboard at http://{args.host}:{args.port}")
    serve_dashboard(conn, host=args.host, port=args.port, report_dir=args.report_dir)


def cmd_runbook(args: argparse.Namespace) -> None:
    print("MYOS Operational Runbook")
    print("\nFirst setup")
    print("1) myos setup-live --check")
    print("2) myos setup-live --apply")
    print("3) myos doctor --strict && myos migrations verify --strict")
    print("\nDaily startup")
    print("1) myos backup")
    print("2) myos sanity")
    print("3) myos run-day --env-file <path> --meeting-hours <n>")
    print("4) myos brief --meeting-hours <n>")
    print("5) myos dashboard --once --output-html ./data/dashboard.html")
    print("6) myos approve --list")
    print("\nMidday")
    print("- myos at-risk")
    print("- myos stop-doing --capacity <n> --deep-budget <n>")
    print("- myos loop goals")
    print("\nEnd of day")
    print('- myos close-day --mode <maker|hybrid|meeting-heavy|recovery> --note "..."')
    print("- myos report --meeting-hours <n>")
    print("- myos trace cleanup --retention-days 30 --max-rows 5000")
    print("\nWeekly")
    print("- myos weekly-review --days 7")
    print("- myos metrics --days 7")
    print("- myos review-evidence --person self")
    print("- myos execution-receipt list")
    print("\nGo-live activation")
    print("- myos launchd-install --autopilot")
    print("- myos activate --env-file <path> --install-launchd --load-launchd")
    if args.short:
        return
    print("\nTroubleshooting")
    print("- myos onboard")
    print("- myos doctor --strict")
    print("- myos migrations verify --strict")
    print("- myos sync --connector all --env-file <path>")
    print("- myos trace list")
    print("- myos trace rollups")
    print("- myos launchd-uninstall --apply (if launch agent reset needed)")


def cmd_health(_: argparse.Namespace) -> None:
    cli_health.cmd_sanity(argparse.Namespace(strict=False, report_dir=""))
    print()
    cli_health.cmd_doctor(argparse.Namespace(strict=False))


def cmd_status_live(args: argparse.Namespace) -> None:
    """Live terminal status dashboard (L3). Blocks until 'q' or Ctrl-C.

    Requires the [tui] extra (rich>=13). Falls back to a one-shot plain-text
    print when rich is not installed or --once is passed.
    """
    from .tui_dashboard import live_status, status_plain

    once = bool(getattr(args, "once", False))
    interval = int(getattr(args, "interval", 5))
    with connection() as conn:
        if once:
            status_plain(conn)
        else:
            live_status(conn, interval=interval)


def cmd_ui(args: argparse.Namespace) -> None:
    cmd_dashboard(
        argparse.Namespace(
            host="127.0.0.1",
            port=args.port,
            report_dir="",
            once=False,
            output_html="",
            output_graph_json="",
        )
    )
