"""Executive-management CLI commands: team roster, notes, 1:1s, meetings, review drafts.

Extracted out of ``cli.py`` (P0.7 slice) so the god-file shrinks without
changing behavior. All commands stay behind the same approval-gated flow:
external mutations from the resulting drafts still land in the standard
approval queue (``myos approve --list``).
"""

from __future__ import annotations

import argparse

from . import em, watch
from .db import connection


def cmd_team(args: argparse.Namespace) -> None:
    """Add a person to the tracked team roster, or list existing entries."""
    with connection() as conn:
        if getattr(args, "team_action", None) == "add":
            pid = em.upsert_person(conn, args.name, role=args.role, team=args.team, relation=args.relation)
            conn.commit()
            print(f"Saved person #{pid}: {args.name}")
            return
        rows = em.list_team(conn)
        if not rows:
            print('No people tracked yet. Add one: myos team add "<name>" --role ... --relation report')
            return
        print("Team & stakeholders:")
        for r in rows:
            extra = "".join(
                filter(None, [f" — {r['role']}" if r["role"] else "", f" @{r['team']}" if r["team"] else ""])
            )
            print(f"- {r['name']} ({r['relation']}){extra}")


def cmd_note(args: argparse.Namespace) -> None:
    """Route a freeform note through the EM inference pipeline into the right bucket."""
    with connection() as conn:
        res = em.route_note(conn, args.text)
        conn.commit()
    routed = res.pop("routed", "inbox")
    detail = ", ".join(f"{k}={v}" for k, v in res.items() if k not in ("created",))
    print(f"Inferred and routed → {routed}" + (f" ({detail})" if detail else ""))


def cmd_one_on_one(args: argparse.Namespace) -> None:
    """Log a 1:1 conversation and extract action items into the standard inbox."""
    with connection() as conn:
        res = em.log_one_on_one(conn, args.person, args.notes)
        conn.commit()
    print(
        f"Logged 1:1 #{res['one_on_one_id']} with {args.person}; "
        f"{len(res['action_item_ids'])} action item(s) captured to your inbox."
    )


def cmd_meeting(args: argparse.Namespace) -> None:
    """Capture a meeting (typed or audio transcript) and extract action items."""
    with connection() as conn:
        text = args.text or ""
        source = "manual"
        if args.audio:
            from . import voice

            text = voice.transcribe(args.audio) or text
            source = "audio"
            if not text:
                print("No transcript produced (install faster-whisper, or pass notes as text).")
                return
        title = args.title or em._first_sentence(text, 60) or "Meeting"
        res = em.capture_meeting(conn, title, text, source=source)
        conn.commit()
    print(
        f"Captured meeting #{res['meeting_id']} '{title}': "
        f"{res['action_items']} action item(s), {len(res['item_ids'])} item(s) total."
    )


def cmd_review_draft(args: argparse.Namespace) -> None:
    """Print an EM review packet for a named person (context-only, no side effects)."""
    with connection() as conn:
        print(em.build_review_packet(conn, args.person))


def cmd_risk_scan(args: argparse.Namespace) -> None:
    """Surface at-risk project signals and (optionally) draft nudges for approval."""
    with connection() as conn:
        findings = watch.scan_project_risks(conn, risk_threshold=args.risk_threshold, limit=args.limit)
        if not findings:
            print("No project risks detected. (Sync connectors first: myos sync --connector all)")
            return
        print(f"Project risks ({len(findings)}):")
        for f in findings:
            owner = f" — {f['owner']}" if f["owner"] else ""
            print(f"- [{f['severity']}] {f['kind']}: {f['title']} ({f['reason']}){owner}")
        if args.draft_nudges:
            ids = watch.draft_nudges(conn, findings, limit=args.nudge_limit)
            print(f"\nDrafted {len(ids)} nudge(s) for approval: {', '.join('#' + str(i) for i in ids)}")
            print("Review and send (graded autonomy gates external posts): myos approve --list")
