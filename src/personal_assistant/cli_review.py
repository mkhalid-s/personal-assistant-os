from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from . import autonomy
from .db import append_event, connection
from .privacy import apply_privacy_filters
from .pulse import detect_mode


def cmd_close_day(args: argparse.Namespace) -> None:
    with connection() as conn:
        open_count = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status = 'open'").fetchone()["c"]
        high_risk = conn.execute(
            "SELECT COUNT(*) AS c FROM work_items WHERE status = 'open' AND risk_score >= 60"
        ).fetchone()["c"]
        open_intents = conn.execute("SELECT COUNT(*) AS c FROM intents WHERE status = 'open'").fetchone()["c"]
        pending_approvals = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_actions WHERE status = 'proposed'"
        ).fetchone()["c"]
        active_factory_runs = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM factory_runs
            WHERE status IN ('running', 'awaiting_approval', 'execution_ready', 'approved_for_execution')
            """
        ).fetchone()["c"]

        mode = args.mode
        summary = (
            f"Closed day with {open_count} open items and {high_risk} high-risk items at "
            f"{datetime.now().isoformat(timespec='minutes')}"
        )

        conn.execute(
            "INSERT INTO daily_logs (summary, mode, note) VALUES (?, ?, ?)",
            (summary, mode, args.note),
        )
        append_event(
            conn,
            "close_day",
            "daily_log",
            None,
            json.dumps(
                {
                    "mode": mode,
                    "open_items": open_count,
                    "high_risk": high_risk,
                    "open_intents": open_intents,
                    "pending_approvals": pending_approvals,
                    "active_factory_runs": active_factory_runs,
                },
                ensure_ascii=True,
            ),
        )
        conn.commit()

        print("Day closed.")
        print(summary)
        print(f"Open intents: {open_intents}")
        print(f"Pending approvals: {pending_approvals}")
        print(f"Active factory runs: {active_factory_runs}")
        if args.note:
            print(f"Note: {args.note}")


def cmd_morning_brief(args: argparse.Namespace) -> None:
    with connection() as conn:
        print("Morning brief:")
        intents_rows = conn.execute(
            """
            SELECT id, objective, priority
            FROM intents
            WHERE status = 'open'
            ORDER BY priority ASC, id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print("Priorities:")
        if intents_rows:
            for row in intents_rows:
                print(f"- intent #{row['id']} priority={row['priority']} {row['objective']}")
        else:
            print("- none")

        risks = conn.execute(
            """
            SELECT id, title, risk_score, due_date
            FROM work_items
            WHERE status = 'open' AND risk_score >= ?
            ORDER BY risk_score DESC, id DESC
            LIMIT ?
            """,
            (args.risk_threshold, args.limit),
        ).fetchall()
        print("Risks:")
        if risks:
            for row in risks:
                due = row["due_date"] or "no due date"
                print(f"- work_item #{row['id']} risk={row['risk_score']} due={due} {row['title']}")
        else:
            print("- none")

        approvals = conn.execute(
            """
            SELECT id, title, action_type
            FROM agent_actions
            WHERE status = 'proposed'
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print("Pending approvals:")
        if approvals:
            for row in approvals:
                print(f"- action #{row['id']} [{row['action_type']}] {row['title']}")
        else:
            print("- none")

        factory_rows = conn.execute(
            """
            SELECT id, intent_id, mode, workflow_pack, status
            FROM factory_runs
            WHERE status IN ('running', 'awaiting_approval', 'execution_ready', 'approved_for_execution')
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print("Factory runs:")
        if factory_rows:
            for row in factory_rows:
                print(
                    f"- factory #{row['id']} intent=#{row['intent_id']} mode={row['mode']} "
                    f"pack={row['workflow_pack']} status={row['status']}"
                )
        else:
            print("- none")

        evidence_gaps = conn.execute(
            """
            SELECT i.id, i.objective
            FROM intents i
            LEFT JOIN intent_evidence e ON e.intent_id = i.id
            WHERE i.status = 'open'
            GROUP BY i.id
            HAVING COUNT(e.id) = 0
            ORDER BY i.priority ASC, i.id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print("Evidence gaps:")
        if evidence_gaps:
            for row in evidence_gaps:
                print(f"- intent #{row['id']} needs evidence: {row['objective']}")
        else:
            print("- none")


def cmd_at_risk(args: argparse.Namespace) -> None:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, risk_score, due_date
            FROM work_items
            WHERE status='open' AND risk_score >= ?
            ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
            LIMIT ?
            """,
            (args.threshold, args.limit),
        ).fetchall()
    if not rows:
        print("No at-risk items.")
        return
    print("At-risk items:")
    for row in rows:
        print(f"- #{row['id']} {row['title']} | risk={row['risk_score']} | due={row['due_date'] or 'none'}")


def cmd_waiting_on(args: argparse.Namespace) -> None:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, owner, due_date
            FROM work_items
            WHERE status='open' AND kind IN ('risk', 'commitment') AND owner IS NOT NULL
            ORDER BY COALESCE(due_date, '9999-12-31') ASC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    if not rows:
        print("No waiting-on style items found.")
        return
    for row in rows:
        print(f"- #{row['id']} waiting on {row['owner']}: {row['title']} (due={row['due_date'] or 'none'})")


def cmd_delegation_candidates(args: argparse.Namespace) -> None:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, kind, risk_score
            FROM work_items
            WHERE status='open' AND kind IN ('task', 'commitment')
            ORDER BY priority ASC, risk_score DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    if not rows:
        print("No delegation candidates.")
        return
    print("Delegation candidates:")
    for row in rows:
        print(f"- #{row['id']} [{row['kind']}] {row['title']} (risk={row['risk_score']})")


def cmd_brief(args: argparse.Namespace) -> None:
    with connection() as conn:
        mode = detect_mode(args.meeting_hours)
        open_items = conn.execute(
            """
            SELECT id, title, kind, risk_score, due_date
            FROM work_items
            WHERE status='open'
            ORDER BY priority ASC, risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
            LIMIT ?
            """,
            (args.top,),
        ).fetchall()
        at_risk = conn.execute(
            """
            SELECT id, title, risk_score, due_date
            FROM work_items
            WHERE status='open' AND risk_score >= ?
            ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
            LIMIT 5
            """,
            (args.risk_threshold,),
        ).fetchall()
        waiting = conn.execute(
            """
            SELECT id, title, owner, due_date
            FROM work_items
            WHERE status='open' AND owner IS NOT NULL AND kind IN ('commitment', 'risk')
            ORDER BY COALESCE(due_date, '9999-12-31') ASC
            LIMIT 5
            """
        ).fetchall()

    print(f"Executive brief | mode={mode} | meeting_hours={args.meeting_hours}")
    print("\nTop outcomes:")
    for idx, row in enumerate(open_items[:3], start=1):
        print(
            f"{idx}. #{row['id']} {row['title']} "
            f"(kind={row['kind']}, risk={row['risk_score']}, due={row['due_date'] or 'none'})"
        )
    if not open_items:
        print("- No open items.")

    print("\nAt-risk:")
    if not at_risk:
        print("- None")
    else:
        for row in at_risk:
            print(f"- #{row['id']} {row['title']} (risk={row['risk_score']}, due={row['due_date'] or 'none'})")

    print("\nWaiting-on:")
    if not waiting:
        print("- None")
    else:
        for row in waiting:
            print(f"- #{row['id']} waiting on {row['owner']}: {row['title']} (due={row['due_date'] or 'none'})")

    if mode == "meeting-heavy":
        print("\nGuidance:")
        print("- Convert deep work into 1-2 tiny wins.")
        print("- Prioritize commitments, decisions, and delegation.")
        print("- Use `myos stop-doing` before accepting new work.")


def cmd_stop_doing(args: argparse.Namespace) -> None:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, kind, risk_score, due_date
            FROM work_items
            WHERE status='open'
            ORDER BY priority ASC, risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
            LIMIT 30
            """
        ).fetchall()
        if not rows:
            print("No open items.")
            return

        suggestions: list[tuple[str, int, str]] = []
        capacity = args.capacity
        deep_budget = args.deep_budget
        deep_candidates = 0
        for row in rows:
            title = row["title"].lower()
            is_deep = any(k in title for k in ["implement", "refactor", "design", "migrate", "build"])
            if is_deep:
                deep_candidates += 1
            if row["risk_score"] < args.keep_risk and row["kind"] in ("task", "note"):
                suggestions.append(("defer", row["id"], row["title"]))
            elif row["kind"] == "task" and row["risk_score"] < 55:
                suggestions.append(("delegate", row["id"], row["title"]))

        print(
            f"Stop-doing review | open={len(rows)} capacity={capacity} deep_budget={deep_budget} "
            f"deep_candidates={deep_candidates}"
        )
        if len(rows) > capacity:
            print(f"- Over capacity by {len(rows) - capacity} items. Defer or delegate lowest-impact work.")
        if deep_candidates > deep_budget:
            print(f"- Deep-work overload: {deep_candidates} deep items > budget {deep_budget}.")

        if not suggestions:
            print("- No strong defer/delegate candidates based on current thresholds.")
            return

        print("\nSuggested actions:")
        for action, item_id, title in suggestions[: args.limit]:
            print(f"- {action.upper()}: #{item_id} {title}")
        append_event(
            conn,
            "stop_doing_review",
            "work_item",
            None,
            json.dumps({"suggestions": len(suggestions), "capacity": capacity}, ensure_ascii=True),
        )
        conn.commit()


def cmd_report(args: argparse.Namespace) -> None:
    with connection() as conn:
        mode = detect_mode(args.meeting_hours)
        top_rows = conn.execute(
            """
            SELECT id, title, kind, risk_score, due_date
            FROM work_items
            WHERE status='open'
            ORDER BY priority ASC, risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
            LIMIT 5
            """
        ).fetchall()
        risk_rows = conn.execute(
            """
            SELECT id, title, risk_score, due_date
            FROM work_items
            WHERE status='open' AND risk_score >= ?
            ORDER BY risk_score DESC
            LIMIT 5
            """,
            (args.risk_threshold,),
        ).fetchall()
        sync_rows = conn.execute(
            """
            SELECT connector, last_status, last_success_at, last_error
            FROM sync_state
            ORDER BY connector ASC
            """
        ).fetchall()

    report_dir = Path(args.output_dir) if args.output_dir else Path(__file__).resolve().parents[2] / "data" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = report_dir / f"daily-brief-{ts}.md"

    lines = [
        f"# Daily Brief ({datetime.now().isoformat(timespec='minutes')})",
        "",
        f"- Mode: `{mode}`",
        f"- Meeting hours: `{args.meeting_hours}`",
        "",
        "## Top Outcomes",
    ]
    if top_rows:
        for row in top_rows[:3]:
            lines.append(
                f"- #{row['id']} {row['title']} (kind={row['kind']}, risk={row['risk_score']}, due={row['due_date'] or 'none'})"
            )
    else:
        lines.append("- No open work items.")

    lines.extend(["", "## At-Risk"])
    if risk_rows:
        for row in risk_rows:
            lines.append(f"- #{row['id']} {row['title']} (risk={row['risk_score']}, due={row['due_date'] or 'none'})")
    else:
        lines.append("- None")

    lines.extend(["", "## Connector Health"])
    if sync_rows:
        for row in sync_rows:
            suffix = f" error={row['last_error']}" if row["last_error"] else ""
            lines.append(
                f"- {row['connector']}: status={row['last_status']} last_success={row['last_success_at']}{suffix}"
            )
    else:
        lines.append("- No connector sync state found.")

    report_path.write_text("\n".join(lines) + "\n")
    print(f"Report generated: {report_path}")


def cmd_metrics(args: argparse.Namespace) -> None:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_items,
              (SELECT COUNT(*) FROM work_items WHERE status='open' AND risk_score >= ?) AS at_risk,
              (SELECT COUNT(*) FROM inbox_items WHERE status='new') AS inbox_new,
              (SELECT COUNT(*) FROM external_items) AS external_total,
              (SELECT COUNT(*) FROM event_log WHERE created_at >= datetime('now', ?)) AS recent_events
            """,
            (args.risk_threshold, f"-{args.days} days"),
        ).fetchone()

        mode_rows = conn.execute(
            """
            SELECT mode, COUNT(*) AS c
            FROM daily_logs
            WHERE created_at >= datetime('now', ?)
            GROUP BY mode
            ORDER BY c DESC
            """,
            (f"-{args.days} days",),
        ).fetchall()
        sync_rows = conn.execute(
            """
            SELECT connector, last_status, last_success_at
            FROM sync_state
            ORDER BY connector ASC
            """
        ).fetchall()
        commitment_rows = conn.execute(
            """
            SELECT
              SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
              SUM(CASE WHEN outcome='completed_late' THEN 1 ELSE 0 END) AS late,
              SUM(CASE WHEN outcome='missed' THEN 1 ELSE 0 END) AS missed,
              SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS open_c
            FROM commitment_log
            """
        ).fetchone()
    print(f"KPI snapshot (last {args.days} days):")
    print(f"- open_items={rows['open_items']} at_risk={rows['at_risk']} inbox_new={rows['inbox_new']}")
    print(f"- external_total={rows['external_total']} recent_events={rows['recent_events']}")
    if mode_rows:
        modes = ", ".join(f"{r['mode']}={r['c']}" for r in mode_rows)
        print(f"- mode_distribution: {modes}")
    else:
        print("- mode_distribution: none")
    if sync_rows:
        statuses = ", ".join(f"{r['connector']}:{r['last_status']}" for r in sync_rows)
        print(f"- connector_status: {statuses}")
    else:
        print("- connector_status: none")
    print(
        "- commitment_health: "
        f"on_time={commitment_rows['on_time'] or 0}, "
        f"late={commitment_rows['late'] or 0}, "
        f"missed={commitment_rows['missed'] or 0}, "
        f"open={commitment_rows['open_c'] or 0}"
    )


def cmd_log_evidence(args: argparse.Namespace) -> None:
    with connection() as conn:
        filtered_impact = apply_privacy_filters(conn, args.impact)
        conn.execute(
            """
            INSERT INTO review_evidence (person, category, impact, artifact_link, privacy_level)
            VALUES (?, ?, ?, ?, ?)
            """,
            (args.person, args.category, filtered_impact, args.artifact_link, args.privacy),
        )
        conn.commit()
        print(f"Evidence logged for {args.person}.")


def cmd_review_evidence(args: argparse.Namespace) -> None:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, person, category, impact, artifact_link, privacy_level, created_at
            FROM review_evidence
            WHERE (? = '' OR person = ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.person, args.person, args.limit),
        ).fetchall()
    if not rows:
        print("No review evidence found.")
        return
    print("Review evidence:")
    for row in rows:
        link = row["artifact_link"] or "none"
        print(
            f"- #{row['id']} person={row['person']} category={row['category']} "
            f"privacy={row['privacy_level']} link={link}\n  impact={row['impact']}"
        )


def cmd_resolve_commitment(args: argparse.Namespace) -> None:
    with connection() as conn:
        wi = conn.execute(
            "SELECT id, due_date FROM work_items WHERE id = ?",
            (args.item,),
        ).fetchone()
        if not wi:
            print("Work item not found.")
            return
        due = wi["due_date"]
        outcome = args.outcome
        if args.outcome == "auto":
            if args.resolved_on and due and args.resolved_on > due:
                outcome = "completed_late"
            elif args.resolved_on and due and args.resolved_on <= due:
                outcome = "completed_on_time"
            else:
                outcome = "completed_on_time"

        cur = conn.execute(
            """
            UPDATE commitment_log
            SET resolved_on = ?, outcome = ?, notes = ?
            WHERE work_item_id = ? AND outcome = 'open'
            """,
            (args.resolved_on or date.today().isoformat(), outcome, args.notes, args.item),
        )
        if cur.rowcount == 0:
            conn.execute(
                """
                INSERT INTO commitment_log (work_item_id, promised_on, due_on, resolved_on, outcome, notes)
                VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
                """,
                (args.item, due, args.resolved_on or date.today().isoformat(), outcome, args.notes),
            )
        if outcome in ("completed_on_time", "completed_late"):
            conn.execute("UPDATE work_items SET status='done', updated_at=CURRENT_TIMESTAMP WHERE id = ?", (args.item,))
        elif outcome == "missed":
            conn.execute(
                "UPDATE work_items SET risk_score = MIN(risk_score + 20, 100), updated_at=CURRENT_TIMESTAMP WHERE id = ?",
                (args.item,),
            )
        conn.commit()
        print(f"Commitment #{args.item} resolved with outcome={outcome}.")


def cmd_weekly_review(args: argparse.Namespace) -> None:
    with connection() as conn:
        open_count = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='open'").fetchone()["c"]
        done_count = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE status='done'").fetchone()["c"]
        risk_count = conn.execute(
            "SELECT COUNT(*) AS c FROM work_items WHERE status='open' AND risk_score >= ?",
            (args.risk_threshold,),
        ).fetchone()["c"]
        evidence_count = conn.execute(
            "SELECT COUNT(*) AS c FROM review_evidence WHERE created_at >= datetime('now', ?)",
            (f"-{args.days} days",),
        ).fetchone()["c"]
        intent_count = conn.execute("SELECT COUNT(*) AS c FROM intents WHERE status='open'").fetchone()["c"]
        evidence_gap_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM intents i
            WHERE i.status = 'open'
              AND NOT EXISTS (SELECT 1 FROM intent_evidence e WHERE e.intent_id = i.id)
            """
        ).fetchone()["c"]
        commitment = conn.execute(
            """
            SELECT
              SUM(CASE WHEN outcome='completed_on_time' THEN 1 ELSE 0 END) AS on_time,
              SUM(CASE WHEN outcome='completed_late' THEN 1 ELSE 0 END) AS late,
              SUM(CASE WHEN outcome='missed' THEN 1 ELSE 0 END) AS missed,
              SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS open_c
            FROM commitment_log
            """
        ).fetchone()
    print(f"Weekly review ({args.days}d window):")
    print(f"- open={open_count} done={done_count} at_risk={risk_count}")
    print(f"- open_intents={intent_count} evidence_gaps={evidence_gap_count}")
    print(
        f"- commitments on_time={commitment['on_time'] or 0} "
        f"late={commitment['late'] or 0} missed={commitment['missed'] or 0} open={commitment['open_c'] or 0}"
    )
    print(f"- review evidence captured={evidence_count}")
    if risk_count > args.risk_alert:
        print("- Alert: risk load is high, run `myos stop-doing` and rebalance commitments.")
    if (commitment["missed"] or 0) > 0:
        print("- Alert: missed commitments detected; renegotiate deadlines and update owners.")


def cmd_renegotiate(args: argparse.Namespace) -> None:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, due_date, risk_score, owner
            FROM work_items
            WHERE status='open'
              AND kind IN ('commitment', 'risk')
              AND due_date IS NOT NULL
              AND due_date <= date('now', ?)
            ORDER BY due_date ASC, risk_score DESC
            LIMIT ?
            """,
            (f"+{args.days_ahead} days", args.limit),
        ).fetchall()
        if not rows:
            print("No commitments requiring renegotiation in window.")
            return
        print("Renegotiation candidates:")
        for row in rows:
            owner = row["owner"] or "stakeholder"
            suggested = args.default_extension_days
            print(
                f"- #{row['id']} {row['title']} (due={row['due_date']}, risk={row['risk_score']})\n"
                f'  suggested message: "Hi {owner}, this item is at risk. Proposing a {suggested}-day extension '
                f'or scope reduction. Can we confirm priority and deadline?"'
            )
        append_event(
            conn,
            "renegotiate_review",
            "work_item",
            None,
            json.dumps({"candidates": len(rows), "days_ahead": args.days_ahead}, ensure_ascii=True),
        )
        conn.commit()


def _daily_feedback_suffix(args: argparse.Namespace, label: str) -> str:
    command = getattr(args, "feedback_command", "myos next-action")
    return f' [label={label} command="{command}"]'


def _daily_feedback_score(conn, *, label: str, command: str) -> int:
    key = autonomy.recommendation_key({"label": label, "command": command})
    row = conn.execute(
        """
        SELECT SUM(CASE WHEN useful = 1 THEN 1 ELSE -1 END) AS score
        FROM recommendation_feedback
        WHERE recommendation_key = ?
          AND datetime(created_at) >= datetime('now', ?)
        """,
        (key, f"-{autonomy.DAILY_RECOMMENDATION_FEEDBACK_WINDOW_DAYS} days"),
    ).fetchone()
    raw_score = int(row["score"] or 0) if row else 0
    score_limit = autonomy.DAILY_RECOMMENDATION_FEEDBACK_SCORE_LIMIT
    return max(-score_limit, min(score_limit, raw_score))


def _daily_candidate(
    conn,
    *,
    args: argparse.Namespace,
    label: str,
    base_rank: int,
    line: str,
) -> dict[str, object]:
    command = getattr(args, "feedback_command", "myos next-action")
    feedback_score = _daily_feedback_score(conn, label=label, command=command)
    return {
        "label": label,
        "base_rank": base_rank,
        "feedback_score": feedback_score,
        "score": base_rank + feedback_score,
        "line": line + _daily_feedback_suffix(args, label),
    }


def _print_best_daily_candidate(candidates: list[dict[str, object]]) -> None:
    if not candidates:
        print("- No open items. Capture and triage first.")
        return
    baseline = sorted(candidates, key=lambda item: -int(item["base_rank"]))[0]
    candidates.sort(key=lambda item: (-int(item["score"]), -int(item["base_rank"])))
    winner = candidates[0]
    print(str(winner["line"]))
    if str(winner["label"]) != str(baseline["label"]):
        selected_feedback_score = int(winner["feedback_score"])
        baseline_feedback_score = int(baseline["feedback_score"])
        print(
            "  ranking context: feedback adjusted selection "
            f"from {baseline['label']} to {winner['label']} "
            f"(selected_score={selected_feedback_score:+d} baseline_score={baseline_feedback_score:+d})"
        )


def cmd_next_action(args: argparse.Namespace) -> None:
    with connection() as conn:
        mode = detect_mode(args.meeting_hours)
        risk = conn.execute(
            """
            SELECT id, title, risk_score, due_date
            FROM work_items
            WHERE status='open' AND risk_score >= ?
            ORDER BY risk_score DESC, COALESCE(due_date, '9999-12-31') ASC
            LIMIT 1
            """,
            (args.risk_threshold,),
        ).fetchone()
        waiting = conn.execute(
            """
            SELECT id, title, owner, due_date
            FROM work_items
            WHERE status='open' AND owner IS NOT NULL AND kind IN ('commitment', 'risk')
            ORDER BY COALESCE(due_date, '9999-12-31') ASC
            LIMIT 1
            """
        ).fetchone()
        deep = conn.execute(
            """
            SELECT id, title, kind, risk_score
            FROM work_items
            WHERE status='open' AND kind IN ('task', 'decision', 'commitment')
            ORDER BY priority ASC, risk_score DESC, created_at ASC
            LIMIT 1
            """
        ).fetchone()

        print(f"Next action recommendation (mode={mode}):")
        candidates: list[dict[str, object]] = []
        if mode == "meeting-heavy":
            if waiting:
                candidates.append(
                    _daily_candidate(
                        conn,
                        args=args,
                        label="daily_nudge_owner",
                        base_rank=30,
                        line=(
                            f"- Nudge owner: #{waiting['id']} {waiting['title']} "
                            f"(owner={waiting['owner']}, due={waiting['due_date'] or 'none'})"
                        ),
                    )
                )
            if risk:
                candidates.append(
                    _daily_candidate(
                        conn,
                        args=args,
                        label="daily_reduce_risk",
                        base_rank=28,
                        line=(
                            f"- Renegotiate risk item: #{risk['id']} {risk['title']} "
                            f"(risk={risk['risk_score']}, due={risk['due_date'] or 'none'})"
                        ),
                    )
                )
            if deep:
                candidates.append(
                    _daily_candidate(
                        conn,
                        args=args,
                        label="daily_tiny_win",
                        base_rank=20,
                        line=f"- Keep one tiny win only: #{deep['id']} {deep['title']}",
                    )
                )
            _print_best_daily_candidate(candidates)
            return

        if risk:
            candidates.append(
                _daily_candidate(
                    conn,
                    args=args,
                    label="daily_reduce_risk",
                    base_rank=30,
                    line=(
                        f"- Reduce top risk now: #{risk['id']} {risk['title']} "
                        f"(risk={risk['risk_score']}, due={risk['due_date'] or 'none'})"
                    ),
                )
            )
        if deep:
            candidates.append(
                _daily_candidate(
                    conn,
                    args=args,
                    label="daily_focus_block",
                    base_rank=20,
                    line=f"- Focus block target: #{deep['id']} {deep['title']} (kind={deep['kind']})",
                )
            )
        _print_best_daily_candidate(candidates)
