from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from . import (
    assistant,
    autonomy,
    autonomy_loop,
    cli_agent,
    cli_autonomy,
    cli_autopilot,
    cli_diagnostics,
    cli_factory,
    cli_health,
    cli_knowledge,
    cli_launchd,
    cli_local_data,
    cli_operations,
    cli_planning,
    cli_review,
    cli_runtime,
    cli_setup_live,
    cli_workflow,
    command_registry,
    em,
    factory,
    graphrag,
    model_setup,
    observability,
    providers,
    queries,
    router,
    watch,
)
from . import (
    context as ctx,
)
from .connectors import AhaConnector, ConfluenceConnector, GitHubConnector, JiraConnector
from .db import append_event, connection
from .execution import (
    _execute_agent_action,  # noqa: F401  # re-exported for tests
    _handle_proposals,
)
from .extraction import extract_suggestions

# Helpers extracted out of this module (refactor #12); re-imported so existing
# call sites (and tests importing them from cli) keep working unchanged.
from .inbox import (
    ensure_work_item_node,
    index_chunk,
    insert_inbox_item_dedup,
)
from .ingest.audio import transcribe_audio
from .ingest.image import extract_image_text
from .locks import acquire_lock, release_lock
from .privacy import (
    _file_sha256,
    apply_privacy_filters,
    get_policy_map,
)
from .pulse import run_cycle


def load_env_file(path: str) -> int:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return 0
    loaded = 0
    for line in env_path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and value and not os.getenv(key):
            os.environ[key] = value
            loaded += 1
    return loaded


def _operations_dependencies() -> cli_operations.OperationsDependencies:
    return cli_operations.OperationsDependencies(
        load_env_file=load_env_file,
        orchestrate_command=cmd_orchestrate,
    )


def _setup_live_dependencies() -> cli_setup_live.SetupLiveDependencies:
    return cli_setup_live.SetupLiveDependencies(
        launchd_install_command=cmd_launchd_install,
    )


def _launchd_runtime_dependencies() -> cli_launchd.LaunchdRuntimeDependencies:
    return cli_launchd.LaunchdRuntimeDependencies(
        load_env_file=load_env_file,
        onboard_command=cmd_onboard,
        go_live_command=cmd_go_live,
        launchd_status_command=cmd_launchd_status,
        sanity_command=cmd_sanity,
    )


def _command_path(args: argparse.Namespace) -> str:
    parts = [str(getattr(args, "command", "") or "unknown")]
    for name, value in sorted(vars(args).items()):
        if name.endswith("_action") and isinstance(value, str) and value:
            parts.append(value)
    return " ".join(parts)


def _argv_hash(argv: list[str]) -> str:
    return observability._hash_text("\0".join(argv))  # hashed only; raw args may contain private text


def _trace_enabled_for(args: argparse.Namespace) -> bool:
    # These commands create, move, or select the database itself; opening an
    # observability connection before they run can interfere with their purpose.
    return str(getattr(args, "command", "") or "") not in {"restore", "setup-live"}


def _command_autonomy_decision(
    conn: sqlite3.Connection, command: str, *, requested_mode: str = ""
) -> dict[str, object]:
    return cli_autonomy.command_autonomy_decision(conn, command, requested_mode=requested_mode)


def _print_autonomy_decision(decision: dict[str, object]) -> None:
    cli_autonomy.print_autonomy_decision(decision)


def _print_recommendations(conn: sqlite3.Connection, recommendations: list[dict[str, object]]) -> None:
    cli_autonomy.print_recommendations(conn, recommendations)


def cmd_capture(args: argparse.Namespace) -> None:
    cli_workflow.cmd_capture(args)


def _is_watchable_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file() and path.suffix.lower() in {".txt", ".md", ".markdown", ".log"}


def _scan_watch_dirs(conn, *, limit: int = 20, min_confidence: float = 0.65) -> tuple[int, int]:
    policy = get_policy_map(conn)
    max_file_bytes = int(policy.get("watch_max_file_bytes", str(2 * 1024 * 1024)))
    max_candidates = max(limit * 50, 100)
    watch_dirs = conn.execute(
        """
        SELECT id, path
        FROM assistant_watch_dirs
        WHERE status='active'
        ORDER BY id ASC
        """
    ).fetchall()
    files_ingested = 0
    suggestions_created = 0
    candidates_seen = 0
    for watch_row in watch_dirs:
        root = Path(watch_row["path"]).expanduser()
        if not root.exists() or not root.is_dir():
            continue
        root_resolved = root.resolve()
        for path in root.rglob("*"):
            candidates_seen += 1
            if candidates_seen > max_candidates:
                return files_ingested, suggestions_created
            if files_ingested >= limit:
                return files_ingested, suggestions_created
            if not _is_watchable_file(path):
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root_resolved):
                    continue
                if path.stat().st_size > max_file_bytes:
                    continue
            except OSError:
                continue
            file_hash = _file_sha256(path)
            reserve = conn.execute(
                """
                INSERT OR IGNORE INTO file_ingests (watch_dir_id, file_path, file_hash, status)
                VALUES (?, ?, ?, 'processing')
                """,
                (watch_row["id"], str(path), file_hash),
            )
            if reserve.rowcount == 0:
                continue
            raw_text = path.read_text(errors="replace")
            filtered = apply_privacy_filters(conn, raw_text)
            if not filtered.strip():
                conn.execute(
                    "UPDATE file_ingests SET status='skipped_empty' WHERE file_path=? AND file_hash=?",
                    (str(path), file_hash),
                )
                continue
            conn.execute(
                """
                INSERT INTO media_assets (media_type, file_path, transcript_text, source)
                VALUES ('file', ?, ?, 'watch_dir')
                """,
                (str(path), filtered),
            )
            media_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                "INSERT OR IGNORE INTO media_imports (media_asset_id) VALUES (?)",
                (media_id,),
            )
            conn.execute(
                """
                INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet)
                VALUES ('file', ?, 'watch_dir', '1', 0.75, ?)
                """,
                (str(path), filtered[:400]),
            )
            provenance_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            index_chunk(conn, "media_asset", media_id, filtered, provenance_id=provenance_id)
            for suggestion in extract_suggestions(filtered):
                if suggestion.confidence < min_confidence:
                    continue
                inserted = insert_inbox_item_dedup(
                    conn,
                    text=suggestion.text,
                    kind=suggestion.kind,
                    owner=None,
                    due_date=None,
                    confidence=suggestion.confidence,
                    source=f"watch_file:{file_hash}",
                )
                if inserted is not None:
                    suggestions_created += 1
            conn.execute(
                """
                UPDATE file_ingests
                SET status='ingested', media_asset_id=?
                WHERE file_path=? AND file_hash=?
                """,
                (media_id, str(path), file_hash),
            )
            files_ingested += 1
    return files_ingested, suggestions_created


def cmd_triage(args: argparse.Namespace) -> None:
    cli_workflow.cmd_triage(args)


def cmd_today(args: argparse.Namespace) -> None:
    cli_workflow.cmd_today(args)


def cmd_risk_radar(args: argparse.Namespace) -> None:
    cli_workflow.cmd_risk_radar(args)


def cmd_close_day(args: argparse.Namespace) -> None:
    cli_review.cmd_close_day(args)


def cmd_morning_brief(args: argparse.Namespace) -> None:
    cli_review.cmd_morning_brief(args)


def cmd_transcribe(args: argparse.Namespace) -> None:
    audio_path = args.audio_file
    transcript = transcribe_audio(audio_path, args.text)
    if not transcript:
        print("No transcript produced. Install 'faster-whisper' or provide --text.")
        return

    with connection() as conn:
        filtered = apply_privacy_filters(conn, transcript)
        conn.execute(
            """
            INSERT INTO media_assets (media_type, file_path, transcript_text, source)
            VALUES (?, ?, ?, ?)
            """,
            ("audio", audio_path, filtered, "local"),
        )
        media_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("audio", audio_path, "whisper_or_manual", "1", 0.7 if args.text else 0.82, filtered[:400]),
        )
        provenance_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        index_chunk(conn, "media_asset", media_id, filtered, provenance_id=provenance_id)
        append_event(
            conn,
            "ingest_audio",
            "media_asset",
            media_id,
            json.dumps({"path": audio_path}, ensure_ascii=True),
        )
        conn.commit()
    print(f"Transcript stored as media asset #{media_id}.")
    print("Run: myos inbox-process to generate suggested tasks.")


def cmd_ingest_image(args: argparse.Namespace) -> None:
    image_path = args.image_file
    extracted = extract_image_text(image_path, args.text)
    if not extracted:
        print("Could not extract OCR text. Install tesseract or pass --text manually.")
        return

    with connection() as conn:
        filtered = apply_privacy_filters(conn, extracted)
        conn.execute(
            """
            INSERT INTO media_assets (media_type, file_path, extracted_text, source)
            VALUES (?, ?, ?, ?)
            """,
            ("image", image_path, filtered, "local"),
        )
        media_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("image", image_path, "ocr_or_manual", "1", 0.68 if args.text else 0.8, filtered[:400]),
        )
        provenance_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        index_chunk(conn, "media_asset", media_id, filtered, provenance_id=provenance_id)
        append_event(
            conn,
            "ingest_image",
            "media_asset",
            media_id,
            json.dumps({"path": image_path}, ensure_ascii=True),
        )
        conn.commit()

    print(f"Image text stored as media asset #{media_id}.")
    print('Tip: run `myos context "<topic>"` to retrieve relevant chunks.')


def cmd_link(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_link(args)


def cmd_related(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_related(args)


def cmd_context(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_context(args)


def cmd_retrieval_run(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_retrieval_run(args)


def cmd_recall(args: argparse.Namespace) -> None:
    """Scored recall over the conversation memory: relevance + recency + importance."""
    with connection() as conn:
        hits = ctx.scored_retrieve(conn, args.query, limit=args.limit)
        if hits:
            print(f"Recall for: {args.query}  (score = relevance + recency + importance)")
            for h in hits:
                subj = f" [{h['subject']}]" if h.get("subject") else ""
                print(f"- ({h['score']}) {h['kind']}{subj}: {h['detail']}")
                print(f"    rel={h['relevance']} rec={h['recency']} imp={h['importance']}")
            return
        # No scored observation hit yet — fall back to raw indexed-chunk recall.
        chunks = queries.context_search(conn, args.query, limit=args.limit)
    if not chunks:
        print("No relevant context found.")
        return
    print(f"Context (chunks) for: {args.query}")
    for c in chunks:
        snip = c["snippet"].replace("\n", " ")
        print(f"- ({c['score']}) {c['source_type']}#{c['source_id']}: {snip}")


def cmd_reflect(_: argparse.Namespace) -> None:
    """Distill recent observations into insights + relationship edges, then run hygiene."""
    with connection() as conn:
        r = ctx.reflect(conn)
        h = ctx.hygiene(conn)
        print(
            f"Reflection: {r['insights']} insight(s) across {r['subjects']} subject(s); "
            f"{r.get('suggestions', 0)} new suggestion(s)."
        )
        print(f"Hygiene: merged {h['merged']} duplicate(s), decayed {h['decayed']} stale observation(s).")
        if r.get("suggestions"):
            print("Review them: myos suggestions list")
        rels = ctx.relationships(conn, limit=8)
        if rels:
            print("Top relationships:")
            for rel in rels:
                print(f"- {rel['a']} ↔ {rel['b']} (weight {rel['weight']:.0f})")
        insights = conn.execute(
            "SELECT summary FROM context_insights WHERE superseded_by IS NULL ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
    if insights:
        print("Recent insights:")
        for ins in insights:
            print(f"- {ins['summary']}")


def cmd_suggestions(args: argparse.Namespace) -> None:
    """List / accept / dismiss / apply tracked improvement suggestions (gated — nothing
    executes from here; accepting only records the decision)."""
    with connection() as conn:
        action = getattr(args, "suggestions_action", "list") or "list"
        if action in ("accept", "dismiss", "apply"):
            if args.id is None:
                print(f"Usage: myos suggestions {action} <id>")
                raise SystemExit(1)
            decision = {"accept": "accepted", "dismiss": "dismissed", "apply": "applied"}[action]
            res = ctx.decide_suggestion(conn, args.id, decision, feedback=getattr(args, "feedback", "") or "")
            if res.get("error"):
                print(res["error"])
                raise SystemExit(1)
            print(f"Suggestion #{res['id']} → {res['status']}.")
            return
        rows = ctx.list_suggestions(conn, status=getattr(args, "status", "proposed") or "proposed")
    if not rows:
        print("No open suggestions.")
        return
    print("Improvement suggestions (propose-and-approve; nothing auto-applies):")
    for r in rows:
        print(f"#{r['id']} [{r['status']}] {r['title']}")
        if r.get("rationale"):
            print(f"    why: {r['rationale']}")
    print("Accept: myos suggestions accept <id>   Dismiss: myos suggestions dismiss <id>")


def cmd_memory(_: argparse.Namespace) -> None:
    """One-glance view of what the Context Intelligence Loop has learned."""
    with connection() as conn:
        s = ctx.summary(conn)
        print("MYOS memory & context intelligence")
        print(f"- conversations: {s['conversations']}  turns logged: {s['turns']}")
        print(f"- active observations: {s['observations_active']}  insights: {s['insights']}")
        print(f"- open suggestions: {s['suggestions_open']}  derived relationships: {s['relationships']}")
        rels = ctx.relationships(conn, limit=5)
    if rels:
        print("Strongest relationships:")
        for rel in rels:
            print(f"- {rel['a']} ↔ {rel['b']} (weight {rel['weight']:.0f})")


def cmd_reindex(_: argparse.Namespace) -> None:
    with connection() as conn:
        items = conn.execute("SELECT id, title FROM work_items ORDER BY id ASC").fetchall()

        chunks_added = 0
        nodes_added = 0
        for item in items:
            before = conn.execute(
                "SELECT id FROM knowledge_nodes WHERE node_type = 'work_item' AND ref_id = ?",
                (item["id"],),
            ).fetchone()
            ensure_work_item_node(conn, int(item["id"]), item["title"])
            if not before:
                nodes_added += 1

            has_chunk = conn.execute(
                "SELECT id FROM text_chunks WHERE source_type = 'work_item' AND source_id = ? LIMIT 1",
                (item["id"],),
            ).fetchone()
            # Only increment if a chunk was actually written (index_chunk skips
            # whitespace-only titles; counting them causes a never-ending re-attempt
            # on every future reindex) (review R4-6).
            if not has_chunk and index_chunk(conn, "work_item", int(item["id"]), item["title"]):
                chunks_added += 1

        conn.commit()
    print(f"Reindex complete. Added {nodes_added} nodes and {chunks_added} chunks for existing work items.")


def cmd_sync(args: argparse.Namespace) -> None:
    cli_workflow.cmd_sync(args, load_env_file)


def _sqlite_fts5_available(conn: sqlite3.Connection) -> tuple[bool, str]:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.myos_fts_check USING fts5(content)")
        conn.execute("DROP TABLE temp.myos_fts_check")
        return True, "FTS5 available"
    except sqlite3.Error as exc:
        return False, str(exc)


def _repo_file(path: str) -> Path:
    return Path(__file__).resolve().parents[2] / path


def cmd_doctor(args: argparse.Namespace) -> None:
    cli_health.cmd_doctor(args)


def _check_sqlite_file(path: Path) -> tuple[bool, str]:
    return cli_local_data._check_sqlite_file(path)


def cmd_migrations(args: argparse.Namespace) -> None:
    cli_local_data.cmd_migrations(args)


def cmd_backup(args: argparse.Namespace) -> None:
    cli_local_data.cmd_backup(args)


def cmd_restore(args: argparse.Namespace) -> None:
    cli_local_data.cmd_restore(args)


def _pyproject_dependencies(pyproject: Path) -> list[str]:
    if not pyproject.exists():
        return []
    deps: list[str] = []
    in_deps = False
    for line in pyproject.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("dependencies"):
            if "[" in stripped and "]" in stripped:
                raw = stripped.split("[", 1)[1].rsplit("]", 1)[0]
                return [
                    item.strip().strip("'").strip('"') for item in raw.split(",") if item.strip().strip("'").strip('"')
                ]
            in_deps = True
            continue
        if in_deps and stripped.startswith("]"):
            break
        if in_deps:
            value = stripped.strip(",").strip("'").strip('"')
            if value:
                deps.append(value)
    return deps


def cmd_dependency_check(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    license_file = root / "LICENSE"
    text = pyproject.read_text() if pyproject.exists() else ""
    deps = _pyproject_dependencies(pyproject)
    checks = [
        ("pyproject", pyproject.exists(), str(pyproject)),
        ("license_metadata", "Apache-2.0" in text, "Apache-2.0 in pyproject"),
        ("license_file", license_file.exists() and "Apache License" in license_file.read_text(), str(license_file)),
    ]
    print("Dependency and license check:")
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"- {'PASS' if passed else 'FAIL'} {name}: {detail}")
    print(f"- dependencies={len(deps)}")
    for dep in deps:
        print(f"  - {dep}")
    if args.strict and not ok:
        raise SystemExit(1)


def cmd_performance_baseline(args: argparse.Namespace) -> None:
    with connection() as conn:
        start = time.monotonic()
        hits = graphrag.retrieve(conn, args.query, limit=args.limit)
        retrieval_ms = int((time.monotonic() - start) * 1000)

        start = time.monotonic()
        counts = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM intents WHERE status='open') AS open_intents,
              (SELECT COUNT(*) FROM work_items WHERE status='open') AS open_work,
              (SELECT COUNT(*) FROM agent_actions WHERE status='proposed') AS pending_approvals,
              (SELECT COUNT(*) FROM retrieval_runs) AS retrieval_runs
            """
        ).fetchone()
        summary_ms = int((time.monotonic() - start) * 1000)

    print("Performance baseline:")
    print(f"- retrieval_ms={retrieval_ms} query={args.query!r} hits={len(hits)}")
    print(
        f"- readiness_query_ms={summary_ms} open_intents={counts['open_intents']} "
        f"open_work={counts['open_work']} pending_approvals={counts['pending_approvals']} "
        f"retrieval_runs={counts['retrieval_runs']}"
    )


def cmd_release_check(args: argparse.Namespace) -> None:
    cli_health.cmd_release_check(args)


def cmd_ingest_external(args: argparse.Namespace) -> None:
    cli_workflow.cmd_ingest_external(args)


def cmd_inbox_process(args: argparse.Namespace) -> None:
    cli_workflow.cmd_inbox_process(args)


def cmd_why(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_why(args)


def cmd_at_risk(args: argparse.Namespace) -> None:
    cli_review.cmd_at_risk(args)


def cmd_waiting_on(args: argparse.Namespace) -> None:
    cli_review.cmd_waiting_on(args)


def cmd_delegation_candidates(args: argparse.Namespace) -> None:
    cli_review.cmd_delegation_candidates(args)


def cmd_brief(args: argparse.Namespace) -> None:
    cli_review.cmd_brief(args)


def cmd_stop_doing(args: argparse.Namespace) -> None:
    cli_review.cmd_stop_doing(args)


def cmd_onboard(_: argparse.Namespace) -> None:
    mapping = {
        "jira": (JiraConnector, ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"]),
        "github": (GitHubConnector, ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"]),
        "confluence": (
            ConfluenceConnector,
            ["CONFLUENCE_BASE_URL", "CONFLUENCE_USER_EMAIL", "CONFLUENCE_API_TOKEN"],
        ),
        "aha": (AhaConnector, ["AHA_BASE_URL", "AHA_API_TOKEN"]),
    }
    print("Onboarding diagnostics:")
    ready = 0
    for name, (_, keys) in mapping.items():
        missing = [k for k in keys if not os.getenv(k)]
        if missing:
            print(f"- {name}: MISSING {', '.join(missing)}")
        else:
            print(f"- {name}: READY")
            ready += 1
    print(f"\nConnectors ready: {ready}/{len(mapping)}")
    if ready < len(mapping):
        print("Set missing environment variables, then run: myos sync --connector all")
    else:
        print("All connectors ready. Run: myos run-day --meeting-hours <n>")


def cmd_config_init(args: argparse.Namespace) -> None:
    cli_local_data.cmd_config_init(args)


def _env_template(db_path: Path) -> str:
    return cli_setup_live._env_template(db_path)


def _read_env_values(path: Path) -> dict[str, str]:
    return cli_setup_live._read_env_values(path)


def _upsert_env_lines(path: Path, lines: list[str], *, header: str = "# Managed tiny router model") -> None:
    cli_setup_live._upsert_env_lines(path, lines, header=header)


def _setup_live_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    return cli_setup_live._setup_live_paths(args)


def _env_or_file(key: str, values: dict[str, str]) -> str:
    return cli_setup_live._env_or_file(key, values)


def _cmd_setup_live_check(env_path: Path, db_path: Path, watch_dir: Path) -> bool:
    return cli_setup_live._cmd_setup_live_check(env_path, db_path, watch_dir)


def cmd_setup_live(args: argparse.Namespace) -> None:
    cli_setup_live.cmd_setup_live(args, _setup_live_dependencies())


def cmd_report(args: argparse.Namespace) -> None:
    cli_review.cmd_report(args)


def cmd_run_day(args: argparse.Namespace) -> dict[str, str] | None:
    return cli_operations.cmd_run_day(args, _operations_dependencies())


def cmd_go_live(args: argparse.Namespace) -> None:
    cli_operations.cmd_go_live(args, _operations_dependencies())


def cmd_metrics(args: argparse.Namespace) -> None:
    cli_review.cmd_metrics(args)


def cmd_log_evidence(args: argparse.Namespace) -> None:
    cli_review.cmd_log_evidence(args)


def cmd_review_evidence(args: argparse.Namespace) -> None:
    cli_review.cmd_review_evidence(args)


def cmd_resolve_commitment(args: argparse.Namespace) -> None:
    cli_review.cmd_resolve_commitment(args)


def cmd_weekly_review(args: argparse.Namespace) -> None:
    cli_review.cmd_weekly_review(args)


def cmd_launchd_install(args: argparse.Namespace) -> None:
    cli_launchd.cmd_launchd_install(args)


def cmd_launchd_uninstall(args: argparse.Namespace) -> None:
    cli_launchd.cmd_launchd_uninstall(args)


def cmd_activate(args: argparse.Namespace) -> None:
    cli_launchd.cmd_activate(args, _launchd_runtime_dependencies())


def cmd_launchd_status(args: argparse.Namespace) -> None:
    cli_runtime.cmd_launchd_status(args)


def cmd_start(args: argparse.Namespace) -> None:
    cli_launchd.cmd_start(args, _launchd_runtime_dependencies())


def cmd_stop(args: argparse.Namespace) -> None:
    cli_launchd.cmd_stop(args, _launchd_runtime_dependencies())


def cmd_dashboard(args: argparse.Namespace) -> None:
    cli_runtime.cmd_dashboard(args)


def cmd_sanity(args: argparse.Namespace) -> None:
    cli_health.cmd_sanity(args)


def cmd_runbook(args: argparse.Namespace) -> None:
    cli_runtime.cmd_runbook(args)


def cmd_cleanup(args: argparse.Namespace) -> None:
    cli_local_data.cmd_cleanup(args)


def cmd_renegotiate(args: argparse.Namespace) -> None:
    cli_review.cmd_renegotiate(args)


def cmd_next_action(args: argparse.Namespace) -> None:
    cli_review.cmd_next_action(args)


def cmd_snapshot(args: argparse.Namespace) -> None:
    cli_health.cmd_snapshot(args)


def cmd_morning(args: argparse.Namespace) -> None:
    if not getattr(args, "run_day", False) and not getattr(args, "env_file", ""):
        cmd_morning_brief(args)
        return
    cmd_run_day(
        argparse.Namespace(
            env_file=args.env_file,
            connector="all",
            meeting_hours=args.meeting_hours,
            external_limit=100,
            media_limit=30,
            min_confidence=0.65,
            risk_threshold=60,
            capacity=8,
            deep_budget=3,
            keep_risk=60,
            stop_limit=10,
            output_dir="",
        )
    )


def cmd_now(args: argparse.Namespace) -> None:
    cmd_next_action(
        argparse.Namespace(meeting_hours=args.meeting_hours, risk_threshold=60, feedback_command="myos now")
    )


def cmd_end(_: argparse.Namespace) -> None:
    cmd_close_day(argparse.Namespace(mode="hybrid", note="end-of-day quick close"))
    cmd_report(argparse.Namespace(meeting_hours=0.0, risk_threshold=60, output_dir=""))


def cmd_weekly(_: argparse.Namespace) -> None:
    cmd_orchestrate(
        argparse.Namespace(
            workflow="weekly",
            env_file="",
            connector="all",
            meeting_hours=0.0,
            external_limit=100,
            media_limit=30,
            min_confidence=0.65,
            risk_threshold=60,
            capacity=8,
            deep_budget=3,
            keep_risk=60,
            stop_limit=10,
            output_dir="",
        )
    )


def cmd_live(args: argparse.Namespace) -> None:
    cli_launchd.cmd_live(args, _launchd_runtime_dependencies())


def cmd_health(args: argparse.Namespace) -> None:
    cli_runtime.cmd_health(args)


def cmd_ui(args: argparse.Namespace) -> None:
    cli_runtime.cmd_ui(args)


def cmd_orchestrate(args: argparse.Namespace) -> None:
    cli_operations.cmd_orchestrate(args, _operations_dependencies())


def cmd_workflow_runs(args: argparse.Namespace) -> None:
    cli_operations.cmd_workflow_runs(args)


def cmd_policy(args: argparse.Namespace) -> None:
    with connection() as conn:
        if args.set:
            if "=" not in args.set:
                print("Invalid --set format. Use KEY=VALUE.")
                raise SystemExit(1)
            key, value = args.set.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                print("Policy key cannot be empty.")
                raise SystemExit(1)
            conn.execute(
                """
                INSERT INTO assistant_policies (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (key, value),
            )
            conn.commit()
            print(f"Policy updated: {key}={value}")
            return
        print("Policy settings:")
        for key, value in sorted(get_policy_map(conn).items()):
            print(f"- {key}={value}")


def cmd_queue_add(args: argparse.Namespace) -> None:
    cli_operations.cmd_queue_add(args)


def cmd_worker(args: argparse.Namespace) -> None:
    cli_operations.cmd_worker(args, _operations_dependencies())


def cmd_cutover_check(args: argparse.Namespace) -> None:
    cli_health.cmd_cutover_check(args)


def cmd_uat(args: argparse.Namespace) -> None:
    cli_health.cmd_uat(args)


def _percentile(values: list[int], pct: float) -> int:
    return cli_health._percentile(values, pct)


def cmd_tune(args: argparse.Namespace) -> None:
    cli_health.cmd_tune(args)


def cmd_delegate(args: argparse.Namespace) -> None:
    cli_agent.cmd_delegate(args)


# Paths a harnessed-agent patch may NEVER touch — editing these would let an
# approved diff disable the autonomy gate or hijack hooks on the next run (#4).
def cmd_action_provider(args: argparse.Namespace) -> None:
    cli_agent.cmd_action_provider(args)


def cmd_act(args: argparse.Namespace) -> None:
    cli_agent.cmd_act(args)


def cmd_code(args: argparse.Namespace) -> None:
    cli_agent.cmd_code(args)


def cmd_learn(args: argparse.Namespace) -> None:
    cli_agent.cmd_learn(args)


def cmd_coach(args: argparse.Namespace) -> None:
    cli_agent.cmd_coach(args)


def cmd_agent_status(args: argparse.Namespace) -> None:
    cli_agent.cmd_agent_status(args)


def cmd_do(args: argparse.Namespace) -> None:
    with connection() as conn:
        route_decision = router.route_with_feedback(conn, args.text, surface="do")
        autonomy_decision = router.autonomy_decision_for_route(conn, route_decision)
        _print_autonomy_decision(autonomy_decision)
        _print_recommendations(
            conn,
            autonomy.recommend_next_steps(
                autonomy_decision,
                command="do",
                intent=route_decision.intent,
                workflow_pack=route_decision.workflow_pack,
            ),
        )
        if autonomy_decision["decision"] == autonomy.BLOCKED:
            raise SystemExit(1)
        result = router.execute_route(conn, args.text, surface="do", decision=route_decision)
        result["autonomy"] = autonomy_decision
        conn.commit()
    print(router.summarize_result(result))
    decision = result["decision"]
    if decision.get("requires_confirmation"):
        print("Safety: route is review-first or clarification-oriented; external mutations remain approval-gated.")


def _print_model_plan(plan: dict[str, object]) -> None:
    cli_diagnostics._print_model_plan(plan)


def cmd_model(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_model(args)


def cmd_router(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_router(args)


def cmd_trace(args: argparse.Namespace) -> None:
    cli_diagnostics.cmd_trace(args)


def cmd_autonomy(args: argparse.Namespace) -> None:
    cli_autonomy.cmd_autonomy(args)


def cmd_smart_help(args: argparse.Namespace) -> None:
    inventory = router.command_inventory()
    tier = "workflow" if args.tier == "workflows" else args.tier
    tiers = ["daily", "workflow", "expert", "diagnostic"] if tier == "all" else [tier]
    print("MYOS smart command surface")
    print('Primary: myos chat | myos voice | myos autopilot --factory | myos do "..." | myos approve --list')
    for name in tiers:
        commands = inventory.get(name, [])
        print(f"\n{name.title()} commands:")
        for command in commands:
            print(f"- myos {command}")


def _autopilot_dependencies() -> cli_autopilot.AutopilotCommandDependencies:
    return cli_autopilot.AutopilotCommandDependencies(
        load_env_file=load_env_file,
        cmd_sync=cmd_sync,
        cmd_ingest_external=cmd_ingest_external,
        scan_watch_dirs=_scan_watch_dirs,
        cmd_inbox_process=cmd_inbox_process,
        cmd_triage=cmd_triage,
        print_goal_cycle_result=_print_goal_cycle_result,
    )


def _run_autopilot_cycle(args: argparse.Namespace) -> dict[str, int]:
    return cli_autopilot.run_autopilot_cycle(args, _autopilot_dependencies())


def _run_autopilot_goal_cycle(args: argparse.Namespace) -> dict[str, object]:
    return cli_autopilot.run_autopilot_goal_cycle(args, _autopilot_dependencies())


def cmd_autopilot(args: argparse.Namespace) -> None:
    cli_autopilot.cmd_autopilot(args, _autopilot_dependencies())


def _print_loop_result(result: dict[str, object]) -> None:
    cli_autonomy.print_loop_result(result)


def _print_goal_cycle_result(result: dict[str, object]) -> None:
    cli_autonomy.print_goal_cycle_result(result)


def cmd_loop(args: argparse.Namespace) -> None:
    cli_autonomy.cmd_loop(args)


def cmd_approve(args: argparse.Namespace) -> None:
    cli_agent.cmd_approve(args)


def cmd_autopilot_status(args: argparse.Namespace) -> None:
    with connection() as conn:
        json_mode = bool(getattr(args, "json", False))
        rows = conn.execute(
            """
            SELECT id, status, started_at, finished_at, summary
            FROM autopilot_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_actions WHERE requires_approval=1 AND status='proposed'"
        ).fetchone()["c"]
        open_tasks = conn.execute("SELECT COUNT(*) AS c FROM agent_tasks WHERE status='open'").fetchone()["c"]
        if json_mode:
            payload = {
                "schema": "myos.autopilot_status.v1",
                "count": len(rows),
                "limit": int(args.limit),
                "runs": [
                    {
                        "id": int(row["id"]),
                        "status": str(row["status"] or ""),
                        "started_at": str(row["started_at"] or ""),
                        "finished_at": str(row["finished_at"] or ""),
                        "summary": str(row["summary"] or ""),
                    }
                    for row in rows
                ],
                "state": {
                    "open_agent_tasks": int(open_tasks),
                    "approvals_pending": int(pending),
                },
            }
            print(json.dumps(payload, ensure_ascii=True))
            return
        if not rows:
            print("No autopilot runs found.")
        else:
            print("Autopilot runs:")
            for row in rows:
                print(
                    f"- run #{row['id']} status={row['status']} started={row['started_at']} "
                    f"finished={row['finished_at'] or 'running'} summary={row['summary'] or ''}"
                )
        print(f"Autopilot state: open_agent_tasks={open_tasks} approvals_pending={pending}")


def cmd_digest(args: argparse.Namespace) -> None:
    json_mode = bool(getattr(args, "json", False))
    with connection() as conn:
        row = None
        if args.id:
            row = conn.execute(
                "SELECT id, title, body, created_at FROM assistant_digests WHERE id = ?",
                (args.id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, title, body, created_at
                FROM assistant_digests
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
    if not row:
        if json_mode:
            print(json.dumps({"schema": "myos.digest.v1", "error": "not_found"}, ensure_ascii=True))
        else:
            print("No assistant digest found. Run `myos autopilot --once` first.")
        return
    if json_mode:
        payload = {
            "schema": "myos.digest.v1",
            "id": int(row["id"]),
            "title": str(row["title"] or ""),
            "created_at": str(row["created_at"] or ""),
            "body": str(row["body"] or "").rstrip(),
        }
        print(json.dumps(payload, ensure_ascii=True))
        return
    if args.title_only:
        print(f"Digest #{row['id']}: {row['title']} ({row['created_at']})")
        return
    print(row["body"].rstrip())


def cmd_goal(args: argparse.Namespace) -> None:
    with connection() as conn:
        if args.goal_action == "add":
            objective = apply_privacy_filters(conn, args.objective)
            context = apply_privacy_filters(conn, args.context)
            conn.execute(
                """
                INSERT INTO assistant_goals (objective, context, cadence_minutes, priority, status)
                VALUES (?, ?, ?, ?, 'active')
                """,
                (objective, context, args.cadence_minutes, args.priority),
            )
            goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.commit()
            print(f"Added assistant goal #{goal_id}: {objective}")
            return
        if args.goal_action == "pause":
            conn.execute(
                "UPDATE assistant_goals SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (args.id,),
            )
            conn.commit()
            print(f"Paused assistant goal #{args.id}.")
            return
        if args.goal_action == "resume":
            conn.execute(
                "UPDATE assistant_goals SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (args.id,),
            )
            conn.commit()
            print(f"Resumed assistant goal #{args.id}.")
            return
        rows = conn.execute(
            """
            SELECT id, objective, status, cadence_minutes, priority, last_evaluated_at
            FROM assistant_goals
            ORDER BY status ASC, priority ASC, id ASC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            print("No assistant goals found.")
            return
        print("Assistant goals:")
        for row in rows:
            print(
                f"- goal #{row['id']} status={row['status']} priority={row['priority']} "
                f"cadence={row['cadence_minutes']}m last={row['last_evaluated_at'] or 'never'} objective={row['objective']}"
            )


def cmd_self_review(_: argparse.Namespace) -> None:
    with connection() as conn:
        policy = get_policy_map(conn)
        checks: list[tuple[str, bool, str]] = []
        active_goals = conn.execute("SELECT COUNT(*) AS c FROM assistant_goals WHERE status='active'").fetchone()["c"]
        recent_runs = conn.execute(
            "SELECT COUNT(*) AS c FROM autopilot_runs WHERE started_at >= datetime('now', '-1 day')"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_actions WHERE requires_approval=1 AND status='proposed'"
        ).fetchone()["c"]
        action_provider = bool(os.getenv("MYOS_ACTION_COMMAND", "").strip())
        ai_provider = bool(os.getenv("MYOS_AI_COMMAND", "").strip())
        connectors_ready = conn.execute("SELECT COUNT(*) AS c FROM sync_state WHERE last_status='ok'").fetchone()["c"]

        checks.append(("standing_goals", active_goals > 0, f"active_goals={active_goals}"))
        checks.append(("autopilot_recent", recent_runs > 0, f"runs_24h={recent_runs}"))
        checks.append(("approval_queue", pending < 20, f"pending_approvals={pending}"))
        checks.append(
            (
                "ai_reasoning",
                ai_provider or policy.get("ai_provider") == "local",
                f"ai_command={'yes' if ai_provider else 'no'}",
            )
        )
        checks.append(("action_provider", action_provider, f"action_command={'yes' if action_provider else 'no'}"))
        checks.append(("live_connectors", connectors_ready > 0, f"connectors_ok={connectors_ready}"))

        missing = [name for name, ok, _ in checks if not ok]
        status = "ready" if not missing else "needs_setup"
        summary = ", ".join(f"{name}={'ok' if ok else 'missing'}" for name, ok, _ in checks)
        conn.execute(
            """
            INSERT INTO assistant_self_reviews (status, summary, missing_capabilities_json)
            VALUES (?, ?, ?)
            """,
            (status, summary, json.dumps(missing, ensure_ascii=True)),
        )
        conn.commit()
    print(f"Autonomy self-review: {status}")
    for name, ok, detail in checks:
        print(f"- {'PASS' if ok else 'GAP'} {name}: {detail}")
    if missing:
        print("Next setup gaps:")
        for item in missing:
            print(f"- {item}")


def cmd_watch_dir(args: argparse.Namespace) -> None:
    with connection() as conn:
        if args.watch_action == "add":
            path = str(Path(args.path).expanduser())
            conn.execute(
                """
                INSERT INTO assistant_watch_dirs (path, label, status, updated_at)
                VALUES (?, ?, 'active', CURRENT_TIMESTAMP)
                ON CONFLICT(path) DO UPDATE SET label=excluded.label, status='active', updated_at=CURRENT_TIMESTAMP
                """,
                (path, args.label),
            )
            conn.commit()
            print(f"Watching directory: {path}")
            return
        if args.watch_action == "pause":
            conn.execute(
                "UPDATE assistant_watch_dirs SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (args.id,),
            )
            conn.commit()
            print(f"Paused watch directory #{args.id}.")
            return
        if args.watch_action == "resume":
            conn.execute(
                "UPDATE assistant_watch_dirs SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (args.id,),
            )
            conn.commit()
            print(f"Resumed watch directory #{args.id}.")
            return
        rows = conn.execute(
            """
            SELECT id, path, label, status
            FROM assistant_watch_dirs
            ORDER BY id ASC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            print("No watch directories configured.")
            return
        print("Watch directories:")
        for row in rows:
            label = f" label={row['label']}" if row["label"] else ""
            print(f"- #{row['id']} status={row['status']}{label} path={row['path']}")


def cmd_watch_scan(args: argparse.Namespace) -> None:
    with connection() as conn:
        files, suggestions = _scan_watch_dirs(conn, limit=args.limit, min_confidence=args.min_confidence)
        conn.commit()
    print(f"Watch scan complete: files_ingested={files}, suggestions_created={suggestions}")


def cmd_intent(args: argparse.Namespace) -> None:
    cli_planning.cmd_intent(args)


def cmd_plan(args: argparse.Namespace) -> None:
    cli_planning.cmd_plan(args)


def cmd_evidence(args: argparse.Namespace) -> None:
    cli_planning.cmd_evidence(args)


def cmd_review_packet(args: argparse.Namespace) -> None:
    cli_planning.cmd_review_packet(args)


def cmd_execution_receipt(args: argparse.Namespace) -> None:
    cli_agent.cmd_execution_receipt(args)


def cmd_agent_run(args: argparse.Namespace) -> None:
    cli_agent.cmd_agent_run(args)


def cmd_factory(args: argparse.Namespace) -> None:
    cli_factory.cmd_factory(args)


def cmd_entity(args: argparse.Namespace) -> None:
    cli_knowledge.cmd_entity(args)


def cmd_relationship(args: argparse.Namespace) -> None:
    cli_knowledge.cmd_relationship(args)


def cmd_claim(args: argparse.Namespace) -> None:
    cli_knowledge.cmd_claim(args)


def cmd_pulse(args: argparse.Namespace) -> None:
    if args.env_file:
        load_env_file(args.env_file)
    if args.once:
        outputs = run_cycle(meeting_hours=args.meeting_hours)
        print("Pulse cycle done:", ", ".join(outputs))
        return
    with connection() as lock_conn:
        owner = f"pulse-{os.getpid()}"
        while True:
            if acquire_lock(lock_conn, "pulse", owner):
                try:
                    outputs = run_cycle(meeting_hours=args.meeting_hours)
                    print(f"[{datetime.now().isoformat(timespec='seconds')}] cycle -> {', '.join(outputs)}")
                finally:
                    release_lock(lock_conn, "pulse", owner)
                    lock_conn.commit()
            else:
                print("pulse: another instance is mid-cycle; skipping this tick.")
            time.sleep(args.interval_sec)


def cmd_chat(args: argparse.Namespace) -> None:
    if getattr(args, "env_file", ""):
        load_env_file(args.env_file)
    with connection() as conn:
        backend = providers.get_backend(args.backend or None)
        ok, detail = backend.available()
        if not ok:
            print(f"Backend '{backend.name}' is not available: {detail}")
            raise SystemExit(1)
        print(f"MYOS chat [{backend.name}] — ask anything; external changes are proposed for your approval.")
        print("Type 'exit' to quit.")
        history: list[dict] = []
        conversation_id: int | None = None
        while True:
            try:
                user = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user:
                continue
            if user.lower() in ("exit", "quit", ":q"):
                break
            result = assistant.run_turn(
                conn,
                user,
                history,
                backend_name=args.backend or None,
                surface="chat",
                conversation_id=conversation_id,
            )
            conversation_id = result.get("conversation_id", conversation_id)
            history = result.get("history", history)
            reply = (result.get("reply") or "").strip()
            if reply:
                print(f"\nmyos> {reply}")
            _handle_proposals(conn, result.get("proposed_action_ids", []))


def cmd_voice(args: argparse.Namespace) -> None:
    from . import voice

    if getattr(args, "env_file", ""):
        load_env_file(args.env_file)
    with connection() as conn:
        backend = providers.get_backend(args.backend or None)
        ok, detail = backend.available()
        if not ok:
            print(f"Backend '{backend.name}' is not available: {detail}")
            raise SystemExit(1)
        print(f"MYOS voice [{backend.name}] — push-to-talk. Ctrl-C to quit.")
        history: list[dict] = []
        conversation_id: int | None = None
        while True:
            try:
                wav = voice.record_push_to_talk()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not wav:
                print("Voice capture unavailable; exiting voice mode.")
                break
            text = voice.transcribe(wav)
            with contextlib.suppress(OSError):
                os.remove(wav)
            if not text:
                print("(heard nothing — try again)")
                continue
            print(f"you> {text}")
            result = assistant.run_turn(
                conn,
                text,
                history,
                backend_name=args.backend or None,
                surface="voice",
                conversation_id=conversation_id,
            )
            conversation_id = result.get("conversation_id", conversation_id)
            history = result.get("history", history)
            reply = (result.get("reply") or "").strip()
            if reply:
                print(f"myos> {reply}")
                if not args.text_reply:
                    voice.speak(reply)
            _handle_proposals(conn, result.get("proposed_action_ids", []))


def cmd_team(args: argparse.Namespace) -> None:
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
    with connection() as conn:
        res = em.route_note(conn, args.text)
        conn.commit()
    routed = res.pop("routed", "inbox")
    detail = ", ".join(f"{k}={v}" for k, v in res.items() if k not in ("created",))
    print(f"Inferred and routed → {routed}" + (f" ({detail})" if detail else ""))


def cmd_one_on_one(args: argparse.Namespace) -> None:
    with connection() as conn:
        res = em.log_one_on_one(conn, args.person, args.notes)
        conn.commit()
    print(
        f"Logged 1:1 #{res['one_on_one_id']} with {args.person}; "
        f"{len(res['action_item_ids'])} action item(s) captured to your inbox."
    )


def cmd_meeting(args: argparse.Namespace) -> None:
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
    with connection() as conn:
        print(em.build_review_packet(conn, args.person))


def cmd_risk_scan(args: argparse.Namespace) -> None:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myos",
        description="Local-first personal assistant OS (CLI).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    do = sub.add_parser("do", help="Route a natural-language request to the right MYOS workflow.")
    do.add_argument("text", help="What you want MYOS to do.")
    do.set_defaults(func=cmd_do)

    smart_help = sub.add_parser("help", help="Show simplified daily/workflow/expert command tiers.")
    smart_help.add_argument(
        "tier", nargs="?", choices=["daily", "workflow", "workflows", "expert", "diagnostic", "all"], default="daily"
    )
    smart_help.set_defaults(func=cmd_smart_help)

    model = sub.add_parser("model", help="Manage optional tiny local models for MYOS routing.")
    model_sub = model.add_subparsers(dest="model_action", required=True)
    model_recommend = model_sub.add_parser("recommend", help="Recommend a small local model for a purpose.")
    model_recommend.add_argument("--purpose", choices=["router"], default="router")
    model_recommend.set_defaults(func=cmd_model)
    model_setup_parser = model_sub.add_parser("setup", help="Plan or apply tiny local model setup.")
    model_setup_parser.add_argument("--router", action="store_true", help="Configure the router intent model.")
    model_setup_parser.add_argument("--runtime", choices=["auto", "ollama", "llama-cpp", "command"], default="auto")
    model_setup_parser.add_argument(
        "--model", choices=list(model_setup.ROUTER_MODELS), default=model_setup.DEFAULT_ROUTER_MODEL
    )
    model_setup_parser.add_argument("--command", default="", help="Custom MYOS_ROUTER_COMMAND for runtime=command.")
    model_setup_parser.add_argument("--apply", action="store_true", help="Pull/download and write local wrapper files.")
    model_setup_parser.set_defaults(func=cmd_model)
    model_status = model_sub.add_parser("status", help="Show router model readiness.")
    model_status.set_defaults(func=cmd_model)

    router_parser = sub.add_parser("router", help="Evaluate and improve smart routing quality.")
    router_sub = router_parser.add_subparsers(dest="router_action", required=True)
    router_eval = router_sub.add_parser("eval", help="Evaluate route fixtures and calibration.")
    router_eval.add_argument("--fixture", default="", help="Optional route eval fixture JSON path.")
    router_eval.add_argument(
        "--model-shadow", action="store_true", help="Compare local model decisions when configured."
    )
    router_eval.add_argument("--no-record", action="store_true", help="Do not persist eval metadata.")
    router_eval.set_defaults(func=cmd_router)
    router_feedback = router_sub.add_parser("feedback", help="Record privacy-safe route correction metadata.")
    router_feedback.add_argument("--event", type=int, required=True, help="smart_route event_log id.")
    router_feedback.add_argument("--expected-intent", choices=list(router.ROUTABLE_INTENTS), required=True)
    router_feedback.add_argument("--note", default="", help="Optional note; stored as hash and length only.")
    router_feedback.set_defaults(func=cmd_router)
    router_overrides = router_sub.add_parser("overrides", help="List active exact-hash route overrides.")
    router_overrides.add_argument("--limit", type=int, default=20)
    router_overrides.set_defaults(func=cmd_router)
    router_commands = router_sub.add_parser(
        "commands",
        help="List router-visible MYOS command metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "List model-safe command metadata for local router prompts.\n"
            "Output includes tiers, intents, safety levels, side effects, required args, examples, and runtime flags.\n"
            "It does not include raw user text."
        ),
    )
    router_commands.add_argument("--tier", choices=list(command_registry.TIERS), default="")
    router_commands.add_argument("--safety", choices=list(command_registry.SAFETY_LEVELS), default="")
    router_commands.add_argument("--intent", choices=list(router.ROUTABLE_INTENTS), default="")
    router_commands.add_argument("--limit", type=int, default=80)
    router_commands.set_defaults(func=cmd_router)

    trace = sub.add_parser("trace", help="Inspect lightweight command and agent execution traces.")
    trace_sub = trace.add_subparsers(dest="trace_action", required=True)
    trace_list = trace_sub.add_parser("list", help="List recent bounded execution traces.")
    trace_list.add_argument("--limit", type=int, default=20)
    trace_list.add_argument("--status", default="")
    trace_list.add_argument("--command", dest="command_filter", default="")
    trace_list.set_defaults(func=cmd_trace)
    trace_cleanup = trace_sub.add_parser("cleanup", help="Roll up and delete old detailed traces.")
    trace_cleanup.add_argument("--retention-days", type=int, default=observability.DEFAULT_RETENTION_DAYS)
    trace_cleanup.add_argument("--max-rows", type=int, default=observability.DEFAULT_MAX_ROWS)
    trace_cleanup.set_defaults(func=cmd_trace)
    trace_rollups = trace_sub.add_parser("rollups", help="Show retained aggregate trace counts.")
    trace_rollups.add_argument("--limit", type=int, default=20)
    trace_rollups.set_defaults(func=cmd_trace)

    autonomy_parser = sub.add_parser("autonomy", help="Evaluate policy and record privacy-safe feedback.")
    autonomy_sub = autonomy_parser.add_subparsers(dest="autonomy_action", required=True)
    autonomy_eval = autonomy_sub.add_parser("eval", help="Evaluate local autonomy decision fixtures.")
    autonomy_eval.add_argument("--level", choices=list(autonomy.LEVELS), default=autonomy.DEFAULT_LEVEL)
    autonomy_eval.add_argument("--no-record", action="store_true", help="Do not persist eval metadata.")
    autonomy_eval.set_defaults(func=cmd_autonomy)
    autonomy_feedback = autonomy_sub.add_parser("feedback", help="Record privacy-safe autonomy decision feedback.")
    autonomy_feedback.add_argument("--trace", type=int, required=True, help="execution_traces id.")
    autonomy_feedback.add_argument("--expected-decision", choices=list(autonomy.DECISIONS), required=True)
    autonomy_feedback.add_argument("--note", default="", help="Optional note; stored as hash and length only.")
    autonomy_feedback.set_defaults(func=cmd_autonomy)
    recommendation_feedback = autonomy_sub.add_parser(
        "recommendation-feedback",
        help="Record privacy-safe feedback on a printed recommendation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Record usefulness feedback for a recommendation printed by MYOS. "
            "For daily recommendations, copy values from output like "
            '[label=daily_reduce_risk command="myos next-action"].\n'
            "Common daily commands: myos next-action or myos now.\n"
            'For approval handoffs, use --label review_approvals --command "myos approve --list".\n'
            'For goal scheduler handoffs, use --label run_goal_cycle --command "myos loop run-goal --goal 1" '
            'or --label review_goals --command "myos goal list".'
        ),
    )
    recommendation_feedback.add_argument("--label", required=True, help="Printed label, e.g. daily_reduce_risk.")
    recommendation_feedback.add_argument(
        "--command",
        dest="recommendation_command",
        default="",
        help="Printed command text, e.g. myos next-action or myos now.",
    )
    recommendation_feedback.add_argument("--decision", choices=["", *list(autonomy.DECISIONS)], default="")
    recommendation_feedback.add_argument("--intent", default="")
    recommendation_feedback.add_argument("--workflow-pack", default="")
    recommendation_feedback.add_argument("--useful", choices=["yes", "no"], required=True)
    recommendation_feedback.add_argument("--note", default="", help="Optional note; stored as hash and length only.")
    recommendation_feedback.set_defaults(func=cmd_autonomy)
    recommendations = autonomy_sub.add_parser(
        "recommendations",
        help="List recommendation feedback ranking summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Show compact feedback summary fields such as surface, recent_score_30d, side_effects, and mixed_recent.\n"
            "Command context is shown; raw notes, note_hash, and note_length are not shown.\n"
            "Goal scheduler labels such as run_goal_cycle and review_goals appear as surface=goal_scheduler with command context.\n"
            "Tiny limits still keep active daily feedback visible."
        ),
    )
    recommendations.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum summary rows to show; tiny limits still keep active daily feedback visible.",
    )
    recommendations.set_defaults(func=cmd_autonomy)

    loop = sub.add_parser("loop", help="Run one bounded durable autonomy loop cycle.")
    loop_sub = loop.add_subparsers(dest="loop_action", required=True)
    loop_start = loop_sub.add_parser("start", help="Start a durable autonomous task loop.")
    loop_start.add_argument("objective")
    loop_start.add_argument("--context", default="")
    loop_start.add_argument(
        "--backend",
        choices=["", "claude", "claude-sdk", "claude-code-sdk", "cursor", "zero", "claude-code", "copilot", "command"],
        default="",
    )
    loop_start.add_argument("--max-actions", type=int, default=autonomy_loop.DEFAULT_MAX_ACTIONS)
    loop_start.add_argument("--mode", choices=list(autonomy_loop.MODES), default="safe")
    loop_start.set_defaults(func=cmd_loop)
    loop_resume = loop_sub.add_parser("resume", help="Run the next bounded cycle for an autonomy loop task.")
    loop_resume.add_argument("--task", type=int, required=True)
    loop_resume.add_argument("--max-actions", type=int, default=autonomy_loop.DEFAULT_MAX_ACTIONS)
    loop_resume.set_defaults(func=cmd_loop)
    loop_status = loop_sub.add_parser("status", help="Show durable autonomy loop status.")
    loop_status.add_argument("--task", type=int)
    loop_status.add_argument("--limit", type=int, default=10)
    loop_status.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    loop_status.set_defaults(func=cmd_loop)
    loop_goals = loop_sub.add_parser(
        "goals",
        help="List eligible goals for a scheduler cycle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "List due goals and print the next handoff command for each one.\n"
            "Feedback labels mark run-goal and goal-review handoffs for privacy-safe calibration.\n"
            "If no goals are eligible, review standing goals with myos goal list.\n"
            "Examples:\n"
            "  myos loop goals\n"
            "  myos loop run-goal --goal 1"
        ),
    )
    loop_goals.add_argument("--limit", type=int, default=5, help="Maximum eligible goals to show.")
    loop_goals.set_defaults(func=cmd_loop)
    loop_run_goal = loop_sub.add_parser(
        "run-goal",
        help="Run one bounded goal-driven autonomy cycle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Start or resume exactly one eligible goal loop, then stop for review.\n"
            "Pending approvals remain gated and are handed off to myos approve --list."
        ),
    )
    loop_run_goal.add_argument("--goal", type=int, help="Run a specific assistant goal id.")
    loop_run_goal.add_argument(
        "--backend",
        choices=["", "claude", "claude-sdk", "claude-code-sdk", "cursor", "zero", "claude-code", "copilot", "command"],
        default="",
        help="Optional reasoning backend for this bounded cycle.",
    )
    loop_run_goal.add_argument(
        "--max-actions",
        type=int,
        default=autonomy_loop.DEFAULT_MAX_ACTIONS,
        help="Maximum proposed actions for the cycle.",
    )
    loop_run_goal.add_argument(
        "--limit", type=int, default=5, help="Maximum eligible goals to consider when --goal is omitted."
    )
    loop_run_goal.set_defaults(func=cmd_loop)
    loop_ledger = loop_sub.add_parser(
        "ledger",
        help="Inspect recent autonomy run ledger decisions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Inspect the read-only audit trail for bounded loop and goal scheduler decisions.\n"
            "Filter by goal, task, or status; pending approval rows point to myos approve --list.\n"
            "Examples:\n"
            "  myos loop ledger --status waiting_approval\n"
            "  myos loop ledger --status skipped --goal 1"
        ),
    )
    loop_ledger.add_argument("--limit", type=int, default=20, help="Maximum ledger rows to show.")
    loop_ledger.add_argument("--goal", type=int, help="Show ledger rows for one assistant goal id.")
    loop_ledger.add_argument("--task", type=int, help="Show ledger rows for one autonomy loop task id.")
    loop_ledger.add_argument(
        "--status",
        choices=list(autonomy_loop.LEDGER_STATUSES),
        default="",
        help="Filter by ledger status.",
    )
    loop_ledger.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    loop_ledger.set_defaults(func=cmd_loop)

    capture = sub.add_parser("capture", help="Capture an inbox item.")
    capture.add_argument("text", help="Raw capture text.")
    capture.add_argument("--kind", choices=["note", "task", "commitment", "decision", "risk"])
    capture.add_argument("--due", help="Due date in YYYY-MM-DD format.")
    capture.add_argument("--owner", help="Owner name.")
    capture.set_defaults(func=cmd_capture)

    triage = sub.add_parser("triage", help="Triage inbox into work items.")
    triage.set_defaults(func=cmd_triage)

    today = sub.add_parser("today", help="Generate today's focus list.")
    today.add_argument("--meeting-hours", type=float, default=0.0)
    today.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    today.set_defaults(func=cmd_today)

    risk = sub.add_parser("risk-radar", help="Show current risk-ranked items.")
    risk.set_defaults(func=cmd_risk_radar)

    close = sub.add_parser("close-day", help="Close day and write summary log.")
    close.add_argument("--mode", choices=["maker", "hybrid", "meeting-heavy", "recovery"], default="hybrid")
    close.add_argument("--note", default="")
    close.set_defaults(func=cmd_close_day)

    transcribe = sub.add_parser("transcribe", help="Transcribe an audio file into indexed context.")
    transcribe.add_argument("audio_file", help="Path to audio file.")
    transcribe.add_argument("--text", default="", help="Optional manual transcript text.")
    transcribe.set_defaults(func=cmd_transcribe)

    image = sub.add_parser("ingest-image", help="OCR an image into indexed context.")
    image.add_argument("image_file", help="Path to image file.")
    image.add_argument("--text", default="", help="Optional manual extracted text.")
    image.set_defaults(func=cmd_ingest_image)

    link = sub.add_parser("link", help="Link two work items in the knowledge graph.")
    link.add_argument("--from-item", type=int, required=True)
    link.add_argument("--to-item", type=int, required=True)
    link.add_argument("--relation", default="relates_to")
    link.add_argument("--weight", type=float, default=1.0)
    link.set_defaults(func=cmd_link)

    related = sub.add_parser("related", help="Show graph-related work items.")
    related.add_argument("--item", type=int, required=True)
    related.add_argument("--limit", type=int, default=10)
    related.set_defaults(func=cmd_related)

    context = sub.add_parser("context", help="Find semantic context from indexed chunks.")
    context.add_argument("query", help="Search query.")
    context.add_argument("--limit", type=int, default=5)
    context.add_argument("--graph", action="store_true", help="Use SQLite GraphRAG retrieval trace.")
    context.add_argument("--graph-hops", type=int, default=1)
    context.set_defaults(func=cmd_context)

    retrieval_run = sub.add_parser("retrieval-run", help="Inspect persisted retrieval traces.")
    retrieval_run.add_argument("retrieval_run_action", nargs="?", choices=["list", "show"], default="list")
    retrieval_run.add_argument("--id", type=int)
    retrieval_run.add_argument("--limit", type=int, default=10)
    retrieval_run.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    retrieval_run.set_defaults(func=cmd_retrieval_run)

    recall = sub.add_parser("recall", help="Scored recall over conversation memory (relevance+recency+importance).")
    recall.add_argument("query", help="What to recall.")
    recall.add_argument("--limit", type=int, default=5)
    recall.set_defaults(func=cmd_recall)

    reflect = sub.add_parser("reflect", help="Distill observations into insights + relationships; run memory hygiene.")
    reflect.set_defaults(func=cmd_reflect)

    suggestions = sub.add_parser("suggestions", help="List/accept/dismiss tracked improvement suggestions.")
    suggestions.add_argument(
        "suggestions_action", nargs="?", choices=["list", "accept", "dismiss", "apply"], default="list"
    )
    suggestions.add_argument("id", nargs="?", type=int)
    suggestions.add_argument("--status", default="proposed")
    suggestions.add_argument("--feedback", default="")
    suggestions.set_defaults(func=cmd_suggestions)

    memory = sub.add_parser("memory", help="Overview of logged conversations, observations, insights, relationships.")
    memory.set_defaults(func=cmd_memory)

    reindex = sub.add_parser("reindex", help="Backfill graph nodes and chunks for existing data.")
    reindex.set_defaults(func=cmd_reindex)

    sync = sub.add_parser("sync", help="Sync external connectors.")
    sync.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    sync.add_argument("--env-file", default="")
    sync.set_defaults(func=cmd_sync)

    config_init = sub.add_parser("config-init", help="Create local env template for connector credentials.")
    config_init.add_argument("--path", default="./.env.myos")
    config_init.add_argument("--force", action="store_true")
    config_init.set_defaults(func=cmd_config_init)

    setup_live = sub.add_parser("setup-live", help="Prepare live Autopilot config, folders, goals, and safe defaults.")
    setup_live.add_argument("--apply", action="store_true")
    setup_live.add_argument("--check", action="store_true")
    setup_live.add_argument("--force", action="store_true")
    setup_live.add_argument("--data-dir", default="")
    setup_live.add_argument("--env-file", default="")
    setup_live.add_argument("--db-path", default="")
    setup_live.add_argument("--watch-dir", default="")
    setup_live.add_argument("--install-launchd", action="store_true")
    setup_live.add_argument("--load-launchd", action="store_true")
    setup_live.add_argument("--autopilot-interval-sec", type=int, default=900)
    setup_live.add_argument("--router-model", action="store_true", help="Also configure the tiny local router model.")
    setup_live.add_argument("--router-runtime", choices=["auto", "ollama", "llama-cpp", "command"], default="auto")
    setup_live.add_argument(
        "--router-model-name", choices=list(model_setup.ROUTER_MODELS), default=model_setup.DEFAULT_ROUTER_MODEL
    )
    setup_live.set_defaults(func=cmd_setup_live)

    onboard = sub.add_parser("onboard", help="Show connector onboarding diagnostics.")
    onboard.set_defaults(func=cmd_onboard)

    doctor = sub.add_parser("doctor", help="Show local system and connector health.")
    doctor.add_argument("--strict", action="store_true", help="Exit non-zero if core local checks fail.")
    doctor.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    doctor.set_defaults(func=cmd_doctor)

    backup = sub.add_parser("backup", help="Create a verified SQLite database backup.")
    backup.add_argument("--output", default="", help="Destination .db file. Defaults to data/backups timestamp.")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="Restore the SQLite database from a backup.")
    restore.add_argument("--from", dest="source", required=True, help="Backup .db file to restore from.")
    restore.set_defaults(func=cmd_restore)

    migrations = sub.add_parser("migrations", help="Inspect and verify schema migration health.")
    migrations.add_argument("migrations_action", nargs="?", choices=["verify", "list"], default="verify")
    migrations.add_argument("--strict", action="store_true", help="Exit non-zero if verification fails.")
    migrations.set_defaults(func=cmd_migrations)

    dependency_check = sub.add_parser("dependency-check", help="Check local dependency and license hygiene.")
    dependency_check.add_argument("--strict", action="store_true")
    dependency_check.set_defaults(func=cmd_dependency_check)

    perf = sub.add_parser("performance-baseline", help="Measure retrieval and readiness query timing.")
    perf.add_argument("--query", default="daily priorities risks approvals")
    perf.add_argument("--limit", type=int, default=5)
    perf.set_defaults(func=cmd_performance_baseline)

    release_check = sub.add_parser("release-check", help="Run local release readiness checks.")
    release_check.add_argument("--strict", action="store_true")
    release_check.add_argument("--verbose", action="store_true")
    release_check.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    release_check.set_defaults(func=cmd_release_check)

    ingest_external = sub.add_parser("ingest-external", help="Ingest synced external items into inbox.")
    ingest_external.add_argument("--limit", type=int, default=100)
    ingest_external.add_argument("--min-risk", type=int, default=55)
    ingest_external.set_defaults(func=cmd_ingest_external)

    process = sub.add_parser("inbox-process", help="Extract suggested inbox items from media assets.")
    process.add_argument("--limit", type=int, default=20)
    process.add_argument("--min-confidence", type=float, default=0.65)
    process.set_defaults(func=cmd_inbox_process)

    why = sub.add_parser("why", help="Explain why a work item exists.")
    why.add_argument("--item", type=int, required=True)
    why.add_argument("--graph", action="store_true", help="Include graph-related evidence and path explanations.")
    why.add_argument("--limit", type=int, default=5)
    why.add_argument("--graph-hops", type=int, default=1)
    why.set_defaults(func=cmd_why)

    at_risk = sub.add_parser("at-risk", help="Show at-risk work items.")
    at_risk.add_argument("--threshold", type=int, default=60)
    at_risk.add_argument("--limit", type=int, default=10)
    at_risk.set_defaults(func=cmd_at_risk)

    waiting = sub.add_parser("waiting-on", help="Show waiting-on items with owners.")
    waiting.add_argument("--limit", type=int, default=10)
    waiting.set_defaults(func=cmd_waiting_on)

    delegate = sub.add_parser("delegation-candidates", help="Show likely delegation candidates.")
    delegate.add_argument("--limit", type=int, default=10)
    delegate.set_defaults(func=cmd_delegation_candidates)

    brief = sub.add_parser("brief", help="Generate executive daily brief.")
    brief.add_argument("--meeting-hours", type=float, default=0.0)
    brief.add_argument("--top", type=int, default=10)
    brief.add_argument("--risk-threshold", type=int, default=60)
    brief.set_defaults(func=cmd_brief)

    stop_doing = sub.add_parser("stop-doing", help="Suggest what to defer/delegate/drop.")
    stop_doing.add_argument("--capacity", type=int, default=8)
    stop_doing.add_argument("--deep-budget", type=int, default=3)
    stop_doing.add_argument("--keep-risk", type=int, default=60)
    stop_doing.add_argument("--limit", type=int, default=10)
    stop_doing.set_defaults(func=cmd_stop_doing)

    report = sub.add_parser("report", help="Generate markdown daily report.")
    report.add_argument("--meeting-hours", type=float, default=0.0)
    report.add_argument("--risk-threshold", type=int, default=60)
    report.add_argument("--output-dir", default="")
    report.set_defaults(func=cmd_report)

    run_day = sub.add_parser("run-day", help="Run autonomous daily pipeline end-to-end.")
    run_day.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    run_day.add_argument("--env-file", default="")
    run_day.add_argument("--meeting-hours", type=float, default=0.0)
    run_day.add_argument("--external-limit", type=int, default=100)
    run_day.add_argument("--media-limit", type=int, default=30)
    run_day.add_argument("--min-confidence", type=float, default=0.65)
    run_day.add_argument("--risk-threshold", type=int, default=60)
    run_day.add_argument("--capacity", type=int, default=8)
    run_day.add_argument("--deep-budget", type=int, default=3)
    run_day.add_argument("--keep-risk", type=int, default=60)
    run_day.add_argument("--stop-limit", type=int, default=10)
    run_day.add_argument("--output-dir", default="")
    run_day.set_defaults(func=cmd_run_day)

    go_live = sub.add_parser("go-live", help="Validate live connectors and run first live ingestion + triage.")
    go_live.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    go_live.add_argument("--env-file", default="")
    go_live.add_argument("--external-limit", type=int, default=100)
    go_live.set_defaults(func=cmd_go_live)

    metrics = sub.add_parser("metrics", help="Show KPI snapshot for assistant health.")
    metrics.add_argument("--days", type=int, default=7)
    metrics.add_argument("--risk-threshold", type=int, default=60)
    metrics.set_defaults(func=cmd_metrics)

    log_evidence = sub.add_parser("log-evidence", help="Log performance/review evidence.")
    log_evidence.add_argument("--person", required=True)
    log_evidence.add_argument("--category", required=True)
    log_evidence.add_argument("--impact", required=True)
    log_evidence.add_argument("--artifact-link", default="")
    log_evidence.add_argument("--privacy", choices=["internal", "confidential", "restricted"], default="internal")
    log_evidence.set_defaults(func=cmd_log_evidence)

    review_evidence = sub.add_parser("review-evidence", help="List review evidence entries.")
    review_evidence.add_argument("--person", default="")
    review_evidence.add_argument("--limit", type=int, default=20)
    review_evidence.set_defaults(func=cmd_review_evidence)

    resolve_commitment = sub.add_parser("resolve-commitment", help="Resolve commitment outcome for a work item.")
    resolve_commitment.add_argument("--item", type=int, required=True)
    resolve_commitment.add_argument(
        "--outcome",
        choices=["auto", "completed_on_time", "completed_late", "missed"],
        default="auto",
    )
    resolve_commitment.add_argument("--resolved-on", default="")
    resolve_commitment.add_argument("--notes", default="")
    resolve_commitment.set_defaults(func=cmd_resolve_commitment)

    weekly = sub.add_parser("weekly-review", help="Generate weekly review health summary.")
    weekly.add_argument("--days", type=int, default=7)
    weekly.add_argument("--risk-threshold", type=int, default=60)
    weekly.add_argument("--risk-alert", type=int, default=5)
    weekly.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    weekly.set_defaults(func=cmd_weekly_review)

    launchd_install = sub.add_parser("launchd-install", help="Install launchd agents for sync/pulse.")
    launchd_install.add_argument("--apply", action="store_true")
    launchd_install.add_argument("--load", action="store_true")
    launchd_install.add_argument("--env-file", default="")
    launchd_install.add_argument("--interval-sec", type=int, default=1800)
    launchd_install.add_argument("--meeting-hours", type=float, default=0.0)
    launchd_install.add_argument("--autopilot", action="store_true")
    launchd_install.add_argument("--autopilot-interval-sec", type=int, default=900)
    launchd_install.set_defaults(func=cmd_launchd_install)

    launchd_uninstall = sub.add_parser("launchd-uninstall", help="Remove launchd agents for sync/pulse.")
    launchd_uninstall.add_argument("--apply", action="store_true")
    launchd_uninstall.set_defaults(func=cmd_launchd_uninstall)

    activate = sub.add_parser("activate", help="Run end-to-end activation flow.")
    activate.add_argument("--env-file", default="")
    activate.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    activate.add_argument("--external-limit", type=int, default=100)
    activate.add_argument("--install-launchd", action="store_true")
    activate.add_argument("--load-launchd", action="store_true")
    activate.set_defaults(func=cmd_activate)

    launchd_status = sub.add_parser("launchd-status", help="Show whether MYOS launch agents are loaded.")
    launchd_status.set_defaults(func=cmd_launchd_status)

    start = sub.add_parser("start", help="Start MYOS runtime and run sanity checks.")
    start.add_argument("--env-file", default="")
    start.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    start.add_argument("--external-limit", type=int, default=100)
    start.add_argument("--report-dir", default="")
    start.add_argument("--install-launchd", action="store_true")
    start.add_argument("--load-launchd", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="Stop MYOS runtime launch agents.")
    stop.set_defaults(func=cmd_stop)

    dashboard = sub.add_parser("dashboard", help="Serve or export local dashboard.")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8787)
    dashboard.add_argument("--report-dir", default="")
    dashboard.add_argument("--once", action="store_true")
    dashboard.add_argument("--output-html", default="")
    dashboard.set_defaults(func=cmd_dashboard)

    sanity = sub.add_parser("sanity", help="Run operational sanity checks.")
    sanity.add_argument("--strict", action="store_true")
    sanity.add_argument("--report-dir", default="")
    sanity.set_defaults(func=cmd_sanity)

    runbook = sub.add_parser("runbook", help="Print daily/weekly operational runbook.")
    runbook.add_argument("--short", action="store_true")
    runbook.set_defaults(func=cmd_runbook)

    cleanup = sub.add_parser("cleanup", help="Archive stale open work items.")
    cleanup.add_argument("--days", type=int, default=30)
    cleanup.add_argument("--limit", type=int, default=100)
    cleanup.set_defaults(func=cmd_cleanup)

    renegotiate = sub.add_parser("renegotiate", help="Show at-risk commitments needing renegotiation.")
    renegotiate.add_argument("--days-ahead", type=int, default=2)
    renegotiate.add_argument("--default-extension-days", type=int, default=3)
    renegotiate.add_argument("--limit", type=int, default=20)
    renegotiate.set_defaults(func=cmd_renegotiate)

    next_action = sub.add_parser("next-action", help="Recommend one highest-value next action.")
    next_action.add_argument("--meeting-hours", type=float, default=0.0)
    next_action.add_argument("--risk-threshold", type=int, default=60)
    next_action.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    next_action.set_defaults(func=cmd_next_action)

    snapshot = sub.add_parser("snapshot", help="Export machine-readable state snapshot as JSON.")
    snapshot.add_argument("--risk-threshold", type=int, default=60)
    snapshot.add_argument("--limit", type=int, default=10)
    snapshot.add_argument("--output", default="")
    snapshot.set_defaults(func=cmd_snapshot)

    orchestrate = sub.add_parser("orchestrate", help="Run a tracked workflow orchestration.")
    orchestrate.add_argument("--workflow", choices=["daily", "weekly", "incident"], required=True)
    orchestrate.add_argument("--env-file", default="")
    orchestrate.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    orchestrate.add_argument("--meeting-hours", type=float, default=0.0)
    orchestrate.add_argument("--external-limit", type=int, default=100)
    orchestrate.add_argument("--media-limit", type=int, default=30)
    orchestrate.add_argument("--min-confidence", type=float, default=0.65)
    orchestrate.add_argument("--risk-threshold", type=int, default=60)
    orchestrate.add_argument("--capacity", type=int, default=8)
    orchestrate.add_argument("--deep-budget", type=int, default=3)
    orchestrate.add_argument("--keep-risk", type=int, default=60)
    orchestrate.add_argument("--stop-limit", type=int, default=10)
    orchestrate.add_argument("--output-dir", default="")
    orchestrate.set_defaults(func=cmd_orchestrate)

    workflow_runs = sub.add_parser("workflow-runs", help="List tracked workflow runs.")
    workflow_runs.add_argument("--limit", type=int, default=20)
    workflow_runs.set_defaults(func=cmd_workflow_runs)

    policy = sub.add_parser("policy", help="View or set privacy/retention policy.")
    policy.add_argument("--set", default="", help="Set one policy value (KEY=VALUE).")
    policy.set_defaults(func=cmd_policy)

    queue_add = sub.add_parser("queue-add", help="Queue a workflow run for worker processing.")
    queue_add.add_argument("--workflow", choices=["daily", "weekly", "incident"], required=True)
    queue_add.add_argument("--payload", default="", help="Optional JSON payload of workflow args.")
    queue_add.set_defaults(func=cmd_queue_add)

    worker = sub.add_parser("worker", help="Process queued workflow jobs.")
    worker.add_argument("--limit", type=int, default=5)
    worker.set_defaults(func=cmd_worker)

    cutover_check = sub.add_parser("cutover-check", help="Check live credential/sync readiness before cutover.")
    cutover_check.set_defaults(func=cmd_cutover_check)

    uat = sub.add_parser("uat", help="Evaluate UAT quality metrics on recent data.")
    uat.add_argument("--days", type=int, default=7)
    uat.add_argument("--risk-threshold", type=int, default=60)
    uat.add_argument("--min-sample", type=int, default=5)
    uat.add_argument("--backlog-warn", type=int, default=15)
    uat.add_argument("--acceptance-warn", type=float, default=60.0)
    uat.add_argument("--risk-focus-warn", type=float, default=20.0)
    uat.set_defaults(func=cmd_uat)

    tune = sub.add_parser("tune", help="Suggest UAT thresholds from recent operating data.")
    tune.add_argument("--days", type=int, default=14)
    tune.add_argument("--apply-policy", action="store_true")
    tune.set_defaults(func=cmd_tune)

    entity = sub.add_parser("entity", help="Extract and list deterministic graph entities.")
    entity_sub = entity.add_subparsers(dest="entity_action", required=True)
    entity_extract = entity_sub.add_parser("extract", help="Extract entities from text and persist them.")
    entity_extract.add_argument("--text", required=True)
    entity_extract.add_argument("--source-type", default="note")
    entity_extract.add_argument("--source-id")
    entity_extract.set_defaults(func=cmd_entity)
    entity_list = entity_sub.add_parser("list", help="List extracted entities.")
    entity_list.add_argument("--type", default="", help="Optional entity type filter.")
    entity_list.add_argument("--limit", type=int, default=50)
    entity_list.set_defaults(func=cmd_entity)

    relationship = sub.add_parser("relationship", help="Extract and list typed entity relationships.")
    relationship_sub = relationship.add_subparsers(dest="relationship_action", required=True)
    relationship_extract = relationship_sub.add_parser("extract", help="Extract typed relationships from text.")
    relationship_extract.add_argument("--text", required=True)
    relationship_extract.add_argument("--source-type", default="note")
    relationship_extract.add_argument("--source-id")
    relationship_extract.set_defaults(func=cmd_relationship)
    relationship_list = relationship_sub.add_parser("list", help="List extracted relationships.")
    relationship_list.add_argument("--type", default="", help="Optional relation type filter.")
    relationship_list.add_argument("--limit", type=int, default=50)
    relationship_list.set_defaults(func=cmd_relationship)

    claim = sub.add_parser("claim", help="Extract and list deterministic claims.")
    claim_sub = claim.add_subparsers(dest="claim_action", required=True)
    claim_extract = claim_sub.add_parser("extract", help="Extract claims from text and persist them.")
    claim_extract.add_argument("--text", required=True)
    claim_extract.add_argument("--source-type", default="note")
    claim_extract.add_argument("--source-id")
    claim_extract.set_defaults(func=cmd_claim)
    claim_list = claim_sub.add_parser("list", help="List extracted claims.")
    claim_list.add_argument("--source-type", default="")
    claim_list.add_argument("--limit", type=int, default=50)
    claim_list.set_defaults(func=cmd_claim)

    intent = sub.add_parser("intent", help="Manage first-class assistant intents.")
    intent_sub = intent.add_subparsers(dest="intent_action", required=True)
    intent_create = intent_sub.add_parser("create", help="Create an intent objective.")
    intent_create.add_argument("objective")
    intent_create.add_argument("--context", default="")
    intent_create.add_argument("--constraint", action="append", default=[])
    intent_create.add_argument("--success", default="")
    intent_create.add_argument("--priority", type=int, default=2)
    intent_create.set_defaults(func=cmd_intent)
    intent_list = intent_sub.add_parser("list", help="List intents.")
    intent_list.add_argument("--status", default="open", help="Status filter, or 'all'.")
    intent_list.add_argument("--limit", type=int, default=20)
    intent_list.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    intent_list.set_defaults(func=cmd_intent)
    intent_show = intent_sub.add_parser("show", help="Show one intent with evidence.")
    intent_show.add_argument("--id", type=int, required=True)
    intent_show.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    intent_show.set_defaults(func=cmd_intent)
    intent_evidence = intent_sub.add_parser("evidence", help="Manage intent evidence.")
    evidence_sub = intent_evidence.add_subparsers(dest="evidence_action", required=True)
    evidence_add = evidence_sub.add_parser("add", help="Add evidence to an intent.")
    evidence_add.add_argument("--id", type=int, required=True)
    evidence_add.add_argument("--text", required=True)
    evidence_add.add_argument("--source-type", default="note")
    evidence_add.add_argument("--source-id")
    evidence_add.add_argument("--summary", default="")
    evidence_add.add_argument("--confidence", type=float, default=0.7)
    evidence_add.set_defaults(func=cmd_intent)

    plan = sub.add_parser("plan", help="Create and inspect intent-tied plans.")
    plan_sub = plan.add_subparsers(dest="plan_action", required=True)
    plan_create = plan_sub.add_parser("create", help="Create a draft plan for an intent.")
    plan_create.add_argument("--intent", type=int, required=True)
    plan_create.add_argument("--title", default="")
    plan_create.add_argument("--assumption", action="append", default=[])
    plan_create.set_defaults(func=cmd_plan)
    plan_show = plan_sub.add_parser("show", help="Show a draft plan with steps, risks, and validations.")
    plan_show.add_argument("--id", type=int, required=True)
    plan_show.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    plan_show.set_defaults(func=cmd_plan)
    plan_list = plan_sub.add_parser("list", help="List draft plans.")
    plan_list.add_argument("--intent", type=int, help="Optional intent id filter.")
    plan_list.add_argument("--status", default="", help="Optional status filter.")
    plan_list.add_argument("--limit", type=int, default=20)
    plan_list.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    plan_list.set_defaults(func=cmd_plan)

    evidence = sub.add_parser("evidence", help="Attach evidence artifacts to intents.")
    evidence_sub = evidence.add_subparsers(dest="evidence_action", required=True)
    evidence_attach = evidence_sub.add_parser("attach", help="Attach a retrieval run to an intent.")
    evidence_attach.add_argument("--intent", type=int, required=True)
    evidence_attach.add_argument("--retrieval-run", type=int, required=True)
    evidence_attach.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    evidence_attach.set_defaults(func=cmd_evidence)
    evidence_sync = evidence_sub.add_parser("sync-external", help="Map synced external items into intent evidence.")
    evidence_sync.add_argument("--intent", type=int, required=True)
    evidence_sync.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    evidence_sync.add_argument("--limit", type=int, default=50)
    evidence_sync.set_defaults(func=cmd_evidence)

    review_packet = sub.add_parser("review-packet", help="Build a review packet for a plan.")
    review_packet.add_argument("--plan", type=int, required=True)
    review_packet.add_argument("--retrieval-run", type=int)
    review_packet.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    review_packet.set_defaults(func=cmd_review_packet)

    factory_parser = sub.add_parser("factory", help="Run review-first AI factory workflows.")
    factory_sub = factory_parser.add_subparsers(dest="factory_action", required=True)
    factory_start = factory_sub.add_parser("start", help="Start a traceable factory run for an intent.")
    factory_start.add_argument("--intent", type=int, required=True)
    factory_start.add_argument("--mode", choices=list(factory.MODES), default="review_first")
    factory_start.add_argument("--pack", choices=list(factory.WORKFLOW_PACKS), default="intent_execution")
    factory_start.add_argument("--executor", choices=["local", "zero"], default="local")
    factory_start.add_argument("--repo", default=".", help="Repository path for coding executors.")
    factory_start.add_argument("--timeout", type=int, default=600, help="Executor timeout in seconds.")
    factory_start.add_argument(
        "--max-turns", type=int, default=0, help="Optional Zero max-turns limit; 0 uses Zero default."
    )
    factory_start.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help="Suggested local verification command to include in the Zero review packet.",
    )
    factory_start.set_defaults(func=cmd_factory)
    factory_status = factory_sub.add_parser("status", help="Show factory run stages and artifacts.")
    factory_status.add_argument("--id", type=int, required=True)
    factory_status.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    factory_status.set_defaults(func=cmd_factory)
    factory_stage = factory_sub.add_parser("run-stage", help="Record or update one factory stage.")
    factory_stage.add_argument("--id", type=int, required=True)
    factory_stage.add_argument("--stage", choices=list(factory.STAGES), required=True)
    factory_stage.add_argument(
        "--status", choices=["pending", "running", "completed", "waiting", "blocked", "failed"], default="completed"
    )
    factory_stage.add_argument("--note", default="")
    factory_stage.set_defaults(func=cmd_factory)
    factory_continue = factory_sub.add_parser("continue", help="Continue the next non-execution pending stage.")
    factory_continue.add_argument("--id", type=int, required=True)
    factory_continue.set_defaults(func=cmd_factory)
    factory_review = factory_sub.add_parser("review", help="Review factory readiness before approval.")
    factory_review.add_argument("--id", type=int, required=True)
    factory_review.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    factory_review.set_defaults(func=cmd_factory)
    factory_list = factory_sub.add_parser("list", help="List recent factory runs.")
    factory_list.add_argument("--status", default="", help="Optional status filter (e.g., proposed, approved).")
    factory_list.add_argument("--limit", type=int, default=20)
    factory_list.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    factory_list.set_defaults(func=cmd_factory)
    factory_approve = factory_sub.add_parser(
        "approve", help="Approve a factory run, optionally handing off to execution gates."
    )
    factory_approve.add_argument("--id", type=int, required=True)
    factory_approve.add_argument("--execute", action="store_true")
    factory_approve.set_defaults(func=cmd_factory)
    factory_policy = factory_sub.add_parser("policy", help="Configure or list factory autonomy policies.")
    factory_policy_sub = factory_policy.add_subparsers(dest="policy_action", required=True)
    factory_policy_set = factory_policy_sub.add_parser("set", help="Set an autonomy policy override.")
    factory_policy_set.add_argument("--mode", choices=list(factory.MODES), required=True)
    factory_policy_set.add_argument("--scope-type", choices=["global", "intent", "goal"], default="global")
    factory_policy_set.add_argument("--scope-id", default="")
    factory_policy_set.add_argument("--connector", default="")
    factory_policy_set.add_argument("--action-type", default="")
    factory_policy_set.set_defaults(func=cmd_factory)
    factory_policy_list = factory_policy_sub.add_parser("list", help="List factory autonomy policies.")
    factory_policy_list.add_argument("--limit", type=int, default=50)
    factory_policy_list.set_defaults(func=cmd_factory)
    factory_learn = factory_sub.add_parser("learn", help="Record the outcome of a factory run.")
    factory_learn.add_argument("--id", type=int, required=True)
    factory_learn.add_argument("--outcome", choices=["success", "partial", "failed"], required=True)
    factory_learn.add_argument("--notes", default="")
    factory_learn.set_defaults(func=cmd_factory)
    factory_retro = factory_sub.add_parser("retrospective", help="Show the latest factory retrospective.")
    factory_retro.add_argument("--id", type=int, required=True)
    factory_retro.set_defaults(func=cmd_factory)
    factory_insights = factory_sub.add_parser("insights", help="Show learned factory patterns.")
    factory_insights.add_argument("--intent", type=int)
    factory_insights.add_argument("--pack", choices=list(factory.WORKFLOW_PACKS), default="")
    factory_insights.add_argument("--limit", type=int, default=20)
    factory_insights.set_defaults(func=cmd_factory)

    delegate = sub.add_parser("delegate", help="Delegate an objective to the autonomous assistant core.")
    delegate.add_argument("objective", help="Outcome or task objective for the assistant.")
    delegate.add_argument("--context", default="", help="Additional context, transcript snippet, or constraints.")
    delegate.add_argument("--constraint", action="append", default=[], help="Repeatable constraint for this task.")
    delegate.add_argument("--mode", choices=["safe", "balanced", "aggressive"], default="safe")
    delegate.add_argument("--priority", type=int, default=2)
    delegate.add_argument("--max-actions", type=int, default=5)
    delegate.add_argument("--analogy-limit", type=int, default=5)
    delegate.add_argument(
        "--to",
        default="",
        help="Harness an external agent CLI (zero|cursor|claude-code|copilot|command) to execute this objective.",
    )
    delegate.set_defaults(func=cmd_delegate)

    code = sub.add_parser("code", help="Delegate a repository coding task to an external coding agent.")
    code.add_argument("objective", help="Coding task objective.")
    code.add_argument("--backend", choices=["zero", "cursor", "claude-code", "copilot", "command"], default="zero")
    code.add_argument("--repo", default=".", help="Git repository path to run the coding task against.")
    code.add_argument("--timeout", type=int, default=600, help="Maximum executor runtime in seconds.")
    code.set_defaults(func=cmd_code)

    act = sub.add_parser("act", help="List, approve, and execute assistant-proposed actions.")
    act.add_argument("--task", type=int)
    act.add_argument("--action", type=int)
    act.add_argument("--list", action="store_true")
    act.add_argument("--approve", action="store_true")
    act.add_argument("--execute", action="store_true")
    act.add_argument("--limit", type=int, default=20)
    act.set_defaults(func=cmd_act)

    learn = sub.add_parser("learn", help="Teach the assistant the outcome of a delegated task.")
    learn.add_argument("--task", type=int, required=True)
    learn.add_argument("--outcome", choices=["success", "partial", "failed"], required=True)
    learn.add_argument("--notes", default="")
    learn.add_argument("--confidence", type=float, default=0.8)
    learn.set_defaults(func=cmd_learn)

    coach = sub.add_parser("coach", help="Get analogy-based coaching from assistant memory.")
    coach.add_argument("query", help="Situation or decision you want help with.")
    coach.add_argument("--limit", type=int, default=5)
    coach.set_defaults(func=cmd_coach)

    agent_run = sub.add_parser("agent-run", help="Run a local bounded agent role for an intent.")
    agent_run.add_argument("--intent", type=int, required=True)
    agent_run.add_argument(
        "--role", choices=["planner", "researcher", "executor", "reviewer", "critic", "summarizer"], required=True
    )
    agent_run.add_argument("--plan", type=int)
    agent_run.add_argument("--retrieval-run", type=int)
    agent_run.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    agent_run.set_defaults(func=cmd_agent_run)

    agent_status = sub.add_parser("agent-status", help="Show assistant tasks, actions, and observations.")
    agent_status.add_argument("--task", type=int)
    agent_status.add_argument("--limit", type=int, default=20)
    agent_status.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    agent_status.set_defaults(func=cmd_agent_status)

    autopilot = sub.add_parser("autopilot", help="Run the always-on intelligent assistant loop.")
    autopilot.add_argument("--env-file", default="")
    autopilot.add_argument("--connector", choices=["all", "jira", "github", "confluence", "aha"], default="all")
    autopilot.add_argument("--once", action="store_true")
    autopilot.add_argument("--interval-sec", type=int, default=900)
    autopilot.add_argument("--max-cycles", type=int, default=0)
    autopilot.add_argument("--mode", choices=["safe", "balanced", "aggressive"], default="safe")
    autopilot.add_argument("--risk-threshold", type=int, default=60)
    autopilot.add_argument("--due-days", type=int, default=2)
    autopilot.add_argument("--signal-limit", type=int, default=10)
    autopilot.add_argument("--max-actions", type=int, default=5)
    autopilot.add_argument("--safe-action-limit", type=int, default=20)
    autopilot.add_argument("--external-limit", type=int, default=100)
    autopilot.add_argument("--media-limit", type=int, default=30)
    autopilot.add_argument("--min-confidence", type=float, default=0.65)
    autopilot.add_argument("--watch-limit", type=int, default=20)
    autopilot.add_argument("--digest-dir", default="")
    autopilot.add_argument("--no-sync", action="store_true")
    autopilot.add_argument("--no-process", action="store_true")
    autopilot.add_argument(
        "--watch-risks",
        action="store_true",
        help="Proactively detect project risks each cycle and draft nudges (approval-gated).",
    )
    autopilot.add_argument(
        "--factory", action="store_true", help="Start or continue one policy-aware factory run this cycle."
    )
    autopilot.add_argument("--factory-mode", choices=list(factory.MODES), default="review_first")
    autopilot.add_argument("--factory-pack", choices=["auto", *list(factory.WORKFLOW_PACKS)], default="auto")
    autopilot.add_argument(
        "--loop-goal",
        action="store_true",
        help="Run one goal-driven autonomy scheduler decision and stop; requires --once.",
    )
    autopilot.add_argument(
        "--loop-goal-id",
        type=int,
        help="Target one active goal for --loop-goal; otherwise pick the next eligible goal.",
    )
    autopilot.add_argument(
        "--loop-goal-limit", type=int, default=5, help="Number of eligible goals to inspect for --loop-goal."
    )
    autopilot.set_defaults(func=cmd_autopilot)

    approve = sub.add_parser("approve", help="Review, approve, and optionally execute autopilot actions.")
    approve.add_argument("--list", action="store_true")
    approve.add_argument("--action", type=int)
    approve.add_argument("--execute", action="store_true")
    approve.add_argument("--limit", type=int, default=20)
    approve.add_argument(
        "--json", action="store_true", help="With --list, emit a single JSON object instead of formatted text."
    )
    approve.add_argument(
        "--stale-only",
        action="store_true",
        dest="stale_only",
        help="With --list, show only approvals in nearing_expiry, expired, tampered, or invalid states.",
    )
    approve.set_defaults(func=cmd_approve)

    receipt = sub.add_parser("execution-receipt", help="Inspect action execution receipts.")
    receipt.add_argument("receipt_action", nargs="?", choices=["list", "show"], default="list")
    receipt.add_argument("--id", type=int)
    receipt.add_argument("--limit", type=int, default=20)
    receipt.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    receipt.set_defaults(func=cmd_execution_receipt)

    autopilot_status = sub.add_parser("autopilot-status", help="Show autopilot runs and pending approvals.")
    autopilot_status.add_argument("--limit", type=int, default=10)
    autopilot_status.add_argument(
        "--json", action="store_true", help="Emit a single JSON object instead of formatted text."
    )
    autopilot_status.set_defaults(func=cmd_autopilot_status)

    digest = sub.add_parser("digest", help="Show latest assistant digest.")
    digest.add_argument("--id", type=int, default=0)
    digest.add_argument("--title-only", action="store_true")
    digest.add_argument("--json", action="store_true", help="Emit a single JSON object instead of formatted text.")
    digest.set_defaults(func=cmd_digest)

    goal = sub.add_parser("goal", help="Manage standing goals that autopilot evaluates automatically.")
    goal_sub = goal.add_subparsers(dest="goal_action", required=True)
    goal_add = goal_sub.add_parser("add", help="Add a standing assistant goal.")
    goal_add.add_argument("objective")
    goal_add.add_argument("--context", default="")
    goal_add.add_argument("--cadence-minutes", type=int, default=1440)
    goal_add.add_argument("--priority", type=int, default=2)
    goal_add.set_defaults(func=cmd_goal)
    goal_list = goal_sub.add_parser("list", help="List assistant goals.")
    goal_list.add_argument("--limit", type=int, default=50)
    goal_list.set_defaults(func=cmd_goal)
    goal_pause = goal_sub.add_parser("pause", help="Pause a standing goal.")
    goal_pause.add_argument("--id", type=int, required=True)
    goal_pause.set_defaults(func=cmd_goal)
    goal_resume = goal_sub.add_parser("resume", help="Resume a standing goal.")
    goal_resume.add_argument("--id", type=int, required=True)
    goal_resume.set_defaults(func=cmd_goal)

    self_review = sub.add_parser("self-review", help="Review whether the assistant is truly autonomous yet.")
    self_review.set_defaults(func=cmd_self_review)

    action_provider = sub.add_parser(
        "action-provider", help="Built-in approved-action provider for MYOS_ACTION_COMMAND."
    )
    action_provider.add_argument(
        "--execute", action="store_true", help="Execute guarded external action instead of dry-run outbox."
    )
    action_provider.set_defaults(func=cmd_action_provider)

    watch_dir = sub.add_parser("watch-dir", help="Manage folders Autopilot ingests automatically.")
    watch_sub = watch_dir.add_subparsers(dest="watch_action", required=True)
    watch_add = watch_sub.add_parser("add", help="Watch a folder for text/markdown transcripts and notes.")
    watch_add.add_argument("path")
    watch_add.add_argument("--label", default="")
    watch_add.set_defaults(func=cmd_watch_dir)
    watch_list = watch_sub.add_parser("list", help="List watched folders.")
    watch_list.add_argument("--limit", type=int, default=50)
    watch_list.set_defaults(func=cmd_watch_dir)
    watch_pause = watch_sub.add_parser("pause", help="Pause a watched folder.")
    watch_pause.add_argument("--id", type=int, required=True)
    watch_pause.set_defaults(func=cmd_watch_dir)
    watch_resume = watch_sub.add_parser("resume", help="Resume a watched folder.")
    watch_resume.add_argument("--id", type=int, required=True)
    watch_resume.set_defaults(func=cmd_watch_dir)

    watch_scan = sub.add_parser("watch-scan", help="Scan watched folders now.")
    watch_scan.add_argument("--limit", type=int, default=20)
    watch_scan.add_argument("--min-confidence", type=float, default=0.65)
    watch_scan.set_defaults(func=cmd_watch_scan)

    morning = sub.add_parser("morning", help="Show start-of-day priorities, risks, approvals, and evidence gaps.")
    morning.add_argument("--env-file", default="")
    morning.add_argument("--meeting-hours", type=float, default=0.0)
    morning.add_argument("--limit", type=int, default=5)
    morning.add_argument("--risk-threshold", type=int, default=60)
    morning.add_argument(
        "--run-day", action="store_true", help="Run the older full run-day workflow instead of the brief."
    )
    morning.set_defaults(func=cmd_morning)

    now = sub.add_parser("now", help="Get one next action now.")
    now.add_argument("--meeting-hours", type=float, default=0.0)
    now.set_defaults(func=cmd_now)

    end = sub.add_parser("end", help="Simple end-of-day close and report.")
    end.set_defaults(func=cmd_end)

    weekly_simple = sub.add_parser("weekly", help="Simple weekly review workflow.")
    weekly_simple.set_defaults(func=cmd_weekly)

    live = sub.add_parser("live", help="Simple live activation flow.")
    live.add_argument("--env-file", default="")
    live.add_argument("--install-launchd", action="store_true")
    live.add_argument("--load-launchd", action="store_true")
    live.set_defaults(func=cmd_live)

    health = sub.add_parser("health", help="Simple health check.")
    health.set_defaults(func=cmd_health)

    ui = sub.add_parser("ui", help="Open simple dashboard server.")
    ui.add_argument("--port", type=int, default=8787)
    ui.set_defaults(func=cmd_ui)

    pulse = sub.add_parser("pulse", help="Run continuous orchestration loop.")
    pulse.add_argument("--env-file", default="")
    pulse.add_argument("--interval-sec", type=int, default=1800)
    pulse.add_argument("--meeting-hours", type=float, default=0.0)
    pulse.add_argument("--once", action="store_true")
    pulse.set_defaults(func=cmd_pulse)

    chat = sub.add_parser("chat", help="Interactive always-on assistant (text). Propose-and-approve.")
    chat.add_argument(
        "--backend", default="", help="claude|copilot|cursor|zero|command (default: MYOS_AGENT_BACKEND or claude)."
    )
    chat.add_argument("--env-file", default="")
    chat.set_defaults(func=cmd_chat)

    voice = sub.add_parser("voice", help="Interactive always-on assistant (push-to-talk voice).")
    voice.add_argument(
        "--backend", default="", help="claude|copilot|cursor|zero|command (default: MYOS_AGENT_BACKEND or claude)."
    )
    voice.add_argument("--env-file", default="")
    voice.add_argument("--text-reply", action="store_true", help="Print replies without speaking them.")
    voice.set_defaults(func=cmd_voice)

    team = sub.add_parser("team", help="List or add team members / stakeholders.")
    team_sub = team.add_subparsers(dest="team_action")
    team_add = team_sub.add_parser("add", help="Add or update a person.")
    team_add.add_argument("name")
    team_add.add_argument("--role", default="")
    team_add.add_argument("--team", default="")
    team_add.add_argument("--relation", choices=["report", "peer", "stakeholder", "manager"], default="report")
    team_add.set_defaults(func=cmd_team)
    team.set_defaults(func=cmd_team)

    note = sub.add_parser(
        "note",
        help="Capture free-form text; MYOS infers what it is (evidence/1:1/meeting/decision/risk/note) and files it.",
    )
    note.add_argument("text")
    note.set_defaults(func=cmd_note)

    one_on_one = sub.add_parser("1on1", help="Log a 1:1; action items are extracted to your inbox.")
    one_on_one.add_argument("--person", required=True)
    one_on_one.add_argument("notes")
    one_on_one.set_defaults(func=cmd_one_on_one)

    meeting = sub.add_parser(
        "meeting", help="Capture a meeting (notes or --audio); decisions + action items extracted."
    )
    meeting.add_argument("text", nargs="?", default="")
    meeting.add_argument("--title", default="")
    meeting.add_argument("--audio", default="", help="Audio file to transcribe (needs faster-whisper).")
    meeting.set_defaults(func=cmd_meeting)

    review_draft = sub.add_parser("review-draft", help="Assemble a performance-review packet for a person.")
    review_draft.add_argument("--person", required=True)
    review_draft.set_defaults(func=cmd_review_draft)

    risk_scan = sub.add_parser(
        "risk-scan", help="Scan synced Jira/GitHub + work items for risks; optionally draft nudges."
    )
    risk_scan.add_argument("--risk-threshold", type=int, default=60)
    risk_scan.add_argument("--limit", type=int, default=25)
    risk_scan.add_argument(
        "--draft-nudges", action="store_true", help="Enqueue a nudge proposal per finding (approval-gated)."
    )
    risk_scan.add_argument("--nudge-limit", type=int, default=10)
    risk_scan.set_defaults(func=cmd_risk_scan)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not _trace_enabled_for(args):
        args.func(args)
        return
    command = str(getattr(args, "command", "") or "unknown")
    command_path = _command_path(args)
    spec = command_registry.find_command(command)
    with connection() as conn:
        correlation_id = observability.start_trace(
            conn,
            command=command,
            command_path=command_path,
            parent_correlation_id=observability.current_correlation_id(),
            argv_hash=_argv_hash(sys.argv[1:]),
        )
        if spec:
            observability.link_trace(
                conn,
                correlation_id,
                intent=spec.intent,
                command_tier=spec.tier,
                safety_level=spec.safety,
            )
            conn.commit()
        previous_trace = os.environ.get(observability.TRACE_ENV)
        os.environ[observability.TRACE_ENV] = correlation_id
        started = time.monotonic()
        status = "completed"
        try:
            args.func(args)
        except SystemExit as exc:
            code = exc.code
            status = "completed" if code in (None, 0) else "failed"
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            observability.finish_trace(
                conn,
                correlation_id,
                status=status,
                duration_ms=duration_ms,
                summary=f"{command_path} {status}",
                metadata={"command_path": command_path},
            )
            if previous_trace is None:
                os.environ.pop(observability.TRACE_ENV, None)
            else:
                os.environ[observability.TRACE_ENV] = previous_trace


if __name__ == "__main__":
    main()
