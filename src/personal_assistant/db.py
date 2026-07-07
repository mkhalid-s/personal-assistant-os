from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

EXPECTED_SCHEMA_VERSION = 37


def resolve_db_path() -> Path:
    raw = os.getenv("MYOS_DB_PATH")
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "assistant.db"


def get_connection() -> sqlite3.Connection:
    db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    initialize_schema(conn)
    return conn


@contextlib.contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Yield an initialized SQLite connection that is guaranteed to close on
    every exit path (return, exception, early ``StopIteration``).

    Prefer this over calling ``get_connection()`` directly: bare
    ``get_connection()`` calls leak the connection whenever a handler returns
    early or raises. CI runs the test suite under ``-W error::ResourceWarning``,
    so a leaked handle is a build failure.

    Example
    -------
        from .db import connection

        def cmd_foo(args):
            with connection() as conn:
                rows = conn.execute(...).fetchall()
                ...
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    current = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").fetchone()["version"]

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inbox_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'note',
            owner TEXT,
            due_date TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            triaged_at TEXT
        );

        CREATE TABLE IF NOT EXISTS work_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id INTEGER,
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            priority INTEGER NOT NULL DEFAULT 2,
            risk_score INTEGER NOT NULL DEFAULT 0,
            owner TEXT,
            due_date TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(inbox_id) REFERENCES inbox_items(id)
        );

        CREATE TABLE IF NOT EXISTS daily_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            mode TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            payload TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS provenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_ref TEXT,
            extractor TEXT NOT NULL DEFAULT 'heuristic:v1',
            extractor_version TEXT NOT NULL DEFAULT '1',
            confidence REAL NOT NULL DEFAULT 0.5,
            snippet TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS extraction_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_ref TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            connector TEXT PRIMARY KEY,
            cursor TEXT,
            last_success_at TEXT,
            last_status TEXT,
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS external_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connector TEXT NOT NULL,
            external_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            owner TEXT,
            status TEXT,
            priority TEXT,
            due_date TEXT,
            url TEXT,
            raw_json TEXT,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(connector, external_id, item_type)
        );

        CREATE TABLE IF NOT EXISTS media_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            transcript_text TEXT,
            extracted_text TEXT,
            source TEXT NOT NULL DEFAULT 'local',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS text_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            provenance_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS knowledge_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_type TEXT NOT NULL,
            ref_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS knowledge_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_node_id INTEGER NOT NULL,
            to_node_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(from_node_id) REFERENCES knowledge_nodes(id),
            FOREIGN KEY(to_node_id) REFERENCES knowledge_nodes(id)
        );

        CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
        CREATE INDEX IF NOT EXISTS idx_work_items_risk ON work_items(risk_score);
        CREATE INDEX IF NOT EXISTS idx_external_connector ON external_items(connector, fetched_at);
        CREATE INDEX IF NOT EXISTS idx_external_status ON external_items(status);
        CREATE INDEX IF NOT EXISTS idx_event_entity ON event_log(entity_type, entity_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON text_chunks(source_type, source_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_ref ON knowledge_nodes(node_type, ref_id);
        CREATE INDEX IF NOT EXISTS idx_edges_from_to ON knowledge_edges(from_node_id, to_node_id);
        """
    )

    if current < 1:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (1, "baseline_with_foundation"),
        )
        current = 1

    if current < 2:
        columns = conn.execute("PRAGMA table_info(text_chunks)").fetchall()
        names = {row["name"] for row in columns}
        if "provenance_id" not in names:
            conn.execute("ALTER TABLE text_chunks ADD COLUMN provenance_id INTEGER")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (2, "add_text_chunks_provenance"),
        )

    if current < 3:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_imports (
                external_item_id INTEGER PRIMARY KEY,
                inbox_id INTEGER NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(external_item_id) REFERENCES external_items(id),
                FOREIGN KEY(inbox_id) REFERENCES inbox_items(id)
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (3, "add_external_imports"),
        )

    if current < 4:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_imports (
                media_asset_id INTEGER PRIMARY KEY,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(media_asset_id) REFERENCES media_assets(id)
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (4, "add_media_imports"),
        )

    if current < 5:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person TEXT NOT NULL,
                category TEXT NOT NULL,
                impact TEXT NOT NULL,
                artifact_link TEXT,
                privacy_level TEXT NOT NULL DEFAULT 'internal',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commitment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_item_id INTEGER NOT NULL,
                promised_on TEXT,
                due_on TEXT,
                resolved_on TEXT,
                outcome TEXT NOT NULL DEFAULT 'open',
                notes TEXT,
                FOREIGN KEY(work_item_id) REFERENCES work_items(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_commitment_outcome ON commitment_log(outcome, due_on)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (5, "add_review_evidence_and_commitment_log"),
        )

    if current < 6:
        # Backfill cleanup for legacy duplicate inbox rows before adding unique index.
        dupes = conn.execute(
            """
            SELECT text, kind, source, MIN(id) AS keep_id
            FROM inbox_items
            GROUP BY text, kind, source
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for row in dupes:
            keep_id = row["keep_id"]
            duplicate_ids = conn.execute(
                """
                SELECT id FROM inbox_items
                WHERE text = ? AND kind = ? AND source = ? AND id <> ?
                """,
                (row["text"], row["kind"], row["source"], keep_id),
            ).fetchall()
            for dup in duplicate_ids:
                dup_id = dup["id"]
                conn.execute("UPDATE work_items SET inbox_id = ? WHERE inbox_id = ?", (keep_id, dup_id))
                conn.execute("UPDATE external_imports SET inbox_id = ? WHERE inbox_id = ?", (keep_id, dup_id))
                conn.execute("DELETE FROM inbox_items WHERE id = ?", (dup_id,))

        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_dedupe ON inbox_items(text, kind, source)")
        work_dupes = conn.execute(
            """
            SELECT inbox_id, MIN(id) AS keep_id
            FROM work_items
            WHERE inbox_id IS NOT NULL
            GROUP BY inbox_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for row in work_dupes:
            keep_id = row["keep_id"]
            duplicate_ids = conn.execute(
                """
                SELECT id FROM work_items
                WHERE inbox_id = ? AND id <> ?
                """,
                (row["inbox_id"], keep_id),
            ).fetchall()
            for dup in duplicate_ids:
                dup_id = dup["id"]
                conn.execute("UPDATE commitment_log SET work_item_id = ? WHERE work_item_id = ?", (keep_id, dup_id))
                conn.execute(
                    "UPDATE text_chunks SET source_id = ? WHERE source_type = 'work_item' AND source_id = ?",
                    (keep_id, dup_id),
                )
                conn.execute(
                    "UPDATE knowledge_nodes SET ref_id = ? WHERE node_type = 'work_item' AND ref_id = ?",
                    (keep_id, dup_id),
                )
                conn.execute("DELETE FROM work_items WHERE id = ?", (dup_id,))

        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_inbox_unique ON work_items(inbox_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_locks (
                name TEXT PRIMARY KEY,
                acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                owner TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (6, "add_idempotency_constraints_and_pipeline_locks"),
        )

    if current < 7:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                summary TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_run_id INTEGER NOT NULL,
                step_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                details TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_runs_name_time ON workflow_runs(workflow_name, started_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_steps_run ON workflow_steps(workflow_run_id, status)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (7, "add_workflow_orchestration_tables"),
        )
    if current < 8:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_policies (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        defaults = [
            ("retention_media_days", "30"),
            ("retention_evidence_days", "365"),
            ("redact_emails", "1"),
            ("redact_phones", "1"),
            ("autonomy_level", "balanced"),
        ]
        for key, value in defaults:
            conn.execute(
                """
                INSERT INTO assistant_policies (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, value),
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (8, "add_policy_settings"),
        )
    if current < 9:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_name TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                last_error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_queue_status_created ON workflow_queue(status, created_at)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (9, "add_workflow_queue"),
        )
    if current < 10:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                objective TEXT NOT NULL,
                context TEXT,
                constraints_json TEXT NOT NULL DEFAULT '{}',
                priority INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_task_id INTEGER NOT NULL,
                agent_name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'local',
                status TEXT NOT NULL DEFAULT 'running',
                plan_json TEXT NOT NULL DEFAULT '[]',
                summary TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                FOREIGN KEY(agent_task_id) REFERENCES agent_tasks(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_task_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'proposed',
                requires_approval INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                executed_at TEXT,
                result TEXT,
                FOREIGN KEY(agent_task_id) REFERENCES agent_tasks(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_task_id INTEGER NOT NULL,
                observation_type TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(agent_task_id) REFERENCES agent_tasks(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status, priority)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_actions_task_status ON agent_actions(agent_task_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_obs_task ON agent_observations(agent_task_id, created_at)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (10, "add_autonomous_assistant_core"),
        )
    if current < 11:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopilot_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                mode TEXT NOT NULL DEFAULT 'safe',
                synced INTEGER NOT NULL DEFAULT 0,
                signals_detected INTEGER NOT NULL DEFAULT 0,
                tasks_created INTEGER NOT NULL DEFAULT 0,
                safe_actions_executed INTEGER NOT NULL DEFAULT 0,
                approvals_pending INTEGER NOT NULL DEFAULT 0,
                summary TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopilot_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_key TEXT NOT NULL UNIQUE,
                signal_type TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER,
                title TEXT NOT NULL,
                detail TEXT,
                status TEXT NOT NULL DEFAULT 'detected',
                agent_task_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(agent_task_id) REFERENCES agent_tasks(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_autopilot_runs_started ON autopilot_runs(started_at, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_autopilot_signals_status ON autopilot_signals(status, created_at)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (11, "add_autopilot_runtime"),
        )
    if current < 12:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_provider_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL DEFAULT 'local',
                purpose TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT,
                response_json TEXT,
                error TEXT,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_provider_calls_time ON ai_provider_calls(created_at, status)")
        defaults = [
            ("ai_provider", "local"),
            ("ai_timeout_sec", "20"),
        ]
        for key, value in defaults:
            conn.execute(
                """
                INSERT INTO assistant_policies (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, value),
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (12, "add_ai_provider_call_tracking"),
        )
    if current < 13:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                autopilot_run_id INTEGER,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(autopilot_run_id) REFERENCES autopilot_runs(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assistant_digests_time ON assistant_digests(created_at)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (13, "add_assistant_digests"),
        )
    if current < 14:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                objective TEXT NOT NULL,
                context TEXT,
                cadence_minutes INTEGER NOT NULL DEFAULT 1440,
                priority INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'active',
                last_evaluated_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_provider_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_action_id INTEGER NOT NULL,
                provider TEXT NOT NULL DEFAULT 'local',
                status TEXT NOT NULL,
                request_json TEXT,
                response_json TEXT,
                error TEXT,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(agent_action_id) REFERENCES agent_actions(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_self_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                missing_capabilities_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_goals_status ON assistant_goals(status, last_evaluated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_provider_executions_action ON action_provider_executions(agent_action_id, created_at)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (14, "add_goals_action_provider_self_review"),
        )
    if current < 15:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_action_id INTEGER,
                provider TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_ref TEXT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'drafted',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_action_outbox_status ON action_outbox(status, created_at)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (15, "add_action_outbox"),
        )
    if current < 16:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_watch_dirs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                label TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_ingests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_dir_id INTEGER,
                file_path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ingested',
                media_asset_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(file_path, file_hash),
                FOREIGN KEY(watch_dir_id) REFERENCES assistant_watch_dirs(id),
                FOREIGN KEY(media_asset_id) REFERENCES media_assets(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_dirs_status ON assistant_watch_dirs(status, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_file_ingests_path ON file_ingests(file_path, created_at)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (16, "add_watch_dirs_file_ingests"),
        )
    if current < 17:
        # FTS5 full-text index over text_chunks for real memory/context retrieval.
        # Best-effort: if this SQLite build lacks FTS5, skip silently and
        # queries.context_search falls back to the brute-force scan.
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS text_chunks_fts "
                "USING fts5(content, source_type UNINDEXED, source_id UNINDEXED)"
            )
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS text_chunks_fts_ai AFTER INSERT ON text_chunks BEGIN
                    INSERT INTO text_chunks_fts(rowid, content, source_type, source_id)
                    VALUES (new.id, new.content, new.source_type, new.source_id);
                END;
                CREATE TRIGGER IF NOT EXISTS text_chunks_fts_ad AFTER DELETE ON text_chunks BEGIN
                    DELETE FROM text_chunks_fts WHERE rowid = old.id;
                END;
                CREATE TRIGGER IF NOT EXISTS text_chunks_fts_au AFTER UPDATE ON text_chunks BEGIN
                    UPDATE text_chunks_fts SET content = new.content WHERE rowid = new.id;
                END;
                """
            )
            conn.execute(
                "INSERT INTO text_chunks_fts(rowid, content, source_type, source_id) "
                "SELECT id, content, source_type, source_id FROM text_chunks "
                "WHERE id NOT IN (SELECT rowid FROM text_chunks_fts)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
                (17, "add_text_chunks_fts5"),
            )
        except sqlite3.OperationalError:
            pass  # FTS5 not compiled in; degrade to scan-based retrieval
    if current < 18:
        # People & performance (P2) + meetings (P3).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                role TEXT,
                team TEXT,
                relation TEXT NOT NULL DEFAULT 'report',
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS one_on_ones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                occurred_on TEXT,
                raw_text TEXT,
                summary TEXT,
                sentiment TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(person_id) REFERENCES people(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS competency_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                competency TEXT NOT NULL,
                level TEXT,
                notes TEXT,
                assessed_on TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(person_id) REFERENCES people(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                occurred_on TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                raw_text TEXT,
                summary TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meeting_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'action',
                text TEXT NOT NULL,
                owner TEXT,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(meeting_id) REFERENCES meetings(id)
            )
            """
        )
        with contextlib.suppress(sqlite3.OperationalError):  # column already added on a prior run
            conn.execute("ALTER TABLE review_evidence ADD COLUMN person_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_one_on_ones_person ON one_on_ones(person_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_competency_person ON competency_snapshots(person_id, assessed_on)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_items_meeting ON meeting_items(meeting_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_evidence_person ON review_evidence(person_id, created_at)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (18, "add_people_perf_meetings"),
        )
    if current < 19:
        # Finding #21: the FTS AFTER UPDATE trigger must also sync source_type/source_id.
        # B2 fix: only DROP/CREATE the trigger when text_chunks_fts actually exists —
        # otherwise we'd install a trigger that crashes on fire AND record version 19
        # while 17 was skipped, stranding 17 forever (search permanently disabled).
        has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_chunks_fts'").fetchone()
        if has_fts:
            conn.execute("DROP TRIGGER IF EXISTS text_chunks_fts_au")
            conn.execute(
                """
                CREATE TRIGGER text_chunks_fts_au AFTER UPDATE ON text_chunks BEGIN
                    UPDATE text_chunks_fts
                       SET content = new.content,
                           source_type = new.source_type,
                           source_id = new.source_id
                     WHERE rowid = new.id;
                END
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
                (19, "fix_fts_au_trigger"),
            )
    if current < 20:
        # Context Intelligence Loop: persist every conversation turn, build an episodic
        # observation stream, distill reflections/insights, and track improvement
        # suggestions with a gated lifecycle. Nothing here mutates external state — it is
        # the memory substrate that lets retrieval and relationship-derivation improve.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                surface TEXT NOT NULL DEFAULT 'chat',
                backend TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_turn_at TEXT,
                turn_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                turn_index INTEGER NOT NULL,
                user_text TEXT,
                assistant_text TEXT,
                backend TEXT,
                proposed_action_ids TEXT,
                latency_ms INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );

            CREATE TABLE IF NOT EXISTS context_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id INTEGER,
                kind TEXT NOT NULL,
                subject TEXT,
                detail TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_accessed_at TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY(turn_id) REFERENCES conversation_turns(id)
            );

            CREATE TABLE IF NOT EXISTS context_insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'reflection',
                subject TEXT,
                summary TEXT NOT NULL,
                evidence_json TEXT,
                confidence REAL NOT NULL DEFAULT 0.5,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                superseded_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS context_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                insight_id INTEGER,
                title TEXT NOT NULL,
                rationale TEXT,
                suggested_action TEXT,
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT,
                feedback TEXT,
                FOREIGN KEY(insight_id) REFERENCES context_insights(id)
            );

            CREATE INDEX IF NOT EXISTS idx_conv_turns_conv ON conversation_turns(conversation_id, turn_index);
            CREATE INDEX IF NOT EXISTS idx_obs_subject ON context_observations(kind, subject, status);
            CREATE INDEX IF NOT EXISTS idx_obs_created ON context_observations(created_at, status);
            CREATE INDEX IF NOT EXISTS idx_suggestions_status ON context_suggestions(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_insights_kind ON context_insights(kind, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (20, "add_context_intelligence_loop"),
        )
    if current < 21:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                objective TEXT NOT NULL,
                context TEXT,
                constraints_json TEXT NOT NULL DEFAULT '[]',
                success_criteria TEXT,
                priority INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS intent_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'note',
                source_id TEXT,
                summary TEXT,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(intent_id) REFERENCES intents(id)
            );

            CREATE TABLE IF NOT EXISTS intent_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                rationale TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                superseded_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(intent_id) REFERENCES intents(id),
                FOREIGN KEY(superseded_by) REFERENCES intent_decisions(id)
            );

            CREATE TABLE IF NOT EXISTS intent_risks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                risk TEXT NOT NULL,
                impact TEXT,
                likelihood TEXT,
                mitigation TEXT,
                owner TEXT,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(intent_id) REFERENCES intents(id)
            );

            CREATE INDEX IF NOT EXISTS idx_intents_status_priority ON intents(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_intent_evidence_intent ON intent_evidence(intent_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_intent_decisions_intent ON intent_decisions(intent_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_intent_risks_intent ON intent_risks(intent_id, status, due_date);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (21, "add_intent_model"),
        )
    if current < 22:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_type, canonical_name)
            );

            CREATE TABLE IF NOT EXISTS entity_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                source_type TEXT,
                source_id TEXT,
                confidence REAL NOT NULL DEFAULT 0.8,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(entity_id) REFERENCES entities(id),
                UNIQUE(entity_id, alias)
            );

            CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities(entity_type, canonical_name);
            CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias);
            CREATE INDEX IF NOT EXISTS idx_entity_aliases_source ON entity_aliases(source_type, source_id);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (22, "add_entities_and_aliases"),
        )
    if current < 23:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_entity_id INTEGER NOT NULL,
                to_entity_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                source_type TEXT,
                source_id TEXT,
                evidence TEXT,
                confidence REAL NOT NULL DEFAULT 0.75,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(from_entity_id) REFERENCES entities(id),
                FOREIGN KEY(to_entity_id) REFERENCES entities(id),
                UNIQUE(from_entity_id, to_entity_id, relation_type, source_type, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships(from_entity_id, relation_type);
            CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships(to_entity_id, relation_type);
            CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_type, source_id);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (23, "add_typed_entity_relationships"),
        )

    if current < 24:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS retrieval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'graph',
                limit_requested INTEGER NOT NULL DEFAULT 5,
                graph_hops INTEGER NOT NULL DEFAULT 1,
                candidate_limit INTEGER NOT NULL DEFAULT 400,
                selected_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS retrieval_run_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                retrieval_run_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                citation TEXT NOT NULL,
                score REAL NOT NULL,
                reason TEXT NOT NULL,
                graph_path_json TEXT NOT NULL DEFAULT '[]',
                content_preview TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(retrieval_run_id) REFERENCES retrieval_runs(id),
                UNIQUE(retrieval_run_id, rank)
            );

            CREATE INDEX IF NOT EXISTS idx_retrieval_runs_time ON retrieval_runs(created_at, mode);
            CREATE INDEX IF NOT EXISTS idx_retrieval_sources_run ON retrieval_run_sources(retrieval_run_id, rank);
            CREATE INDEX IF NOT EXISTS idx_retrieval_sources_source ON retrieval_run_sources(source_type, source_id);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (24, "add_retrieval_run_traces"),
        )

    if current < 25:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                assumptions_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(intent_id) REFERENCES intents(id)
            );

            CREATE TABLE IF NOT EXISTS plan_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                step_index INTEGER NOT NULL,
                description TEXT NOT NULL,
                owner TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                validation TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(plan_id) REFERENCES plans(id),
                UNIQUE(plan_id, step_index)
            );

            CREATE TABLE IF NOT EXISTS plan_risks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                risk TEXT NOT NULL,
                mitigation TEXT,
                severity TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(plan_id) REFERENCES plans(id)
            );

            CREATE TABLE IF NOT EXISTS plan_validations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                check_name TEXT NOT NULL,
                command TEXT,
                expected TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(plan_id) REFERENCES plans(id)
            );

            CREATE TABLE IF NOT EXISTS review_packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                intent_id INTEGER NOT NULL,
                retrieval_run_id INTEGER,
                summary TEXT NOT NULL,
                packet_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(plan_id) REFERENCES plans(id),
                FOREIGN KEY(intent_id) REFERENCES intents(id),
                FOREIGN KEY(retrieval_run_id) REFERENCES retrieval_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_plans_intent ON plans(intent_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id, step_index);
            CREATE INDEX IF NOT EXISTS idx_plan_risks_plan ON plan_risks(plan_id, status);
            CREATE INDEX IF NOT EXISTS idx_plan_validations_plan ON plan_validations(plan_id, status);
            CREATE INDEX IF NOT EXISTS idx_review_packets_plan ON review_packets(plan_id, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (25, "add_plans_and_review_packets"),
        )

    if current < 26:
        columns = conn.execute("PRAGMA table_info(conversation_turns)").fetchall()
        names = {row["name"] for row in columns}
        if "retrieval_run_ids" not in names:
            conn.execute("ALTER TABLE conversation_turns ADD COLUMN retrieval_run_ids TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (26, "add_conversation_turn_retrieval_traces"),
        )

    if current < 27:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_text TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT,
                confidence REAL NOT NULL DEFAULT 0.7,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(claim_text, source_type, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_type, source_id);
            CREATE INDEX IF NOT EXISTS idx_claims_created ON claims(created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (27, "add_claims"),
        )

    if current < 28:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_execution_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_action_id INTEGER NOT NULL,
                agent_task_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                final_status TEXT NOT NULL,
                result TEXT,
                approved INTEGER NOT NULL DEFAULT 0,
                rollback_note TEXT,
                follow_up_required INTEGER NOT NULL DEFAULT 0,
                follow_up_inbox_id INTEGER,
                request_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(agent_action_id) REFERENCES agent_actions(id),
                FOREIGN KEY(agent_task_id) REFERENCES agent_tasks(id),
                FOREIGN KEY(follow_up_inbox_id) REFERENCES inbox_items(id)
            );

            CREATE INDEX IF NOT EXISTS idx_action_receipts_action ON action_execution_receipts(agent_action_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_action_receipts_status ON action_execution_receipts(final_status, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (28, "add_action_execution_receipts"),
        )

    if current < 29:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS factory_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                plan_id INTEGER,
                mode TEXT NOT NULL DEFAULT 'review_first',
                workflow_pack TEXT NOT NULL DEFAULT 'intent_execution',
                executor_backend TEXT NOT NULL DEFAULT 'local',
                executor_context_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'running',
                summary TEXT,
                outcome TEXT,
                outcome_notes TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                FOREIGN KEY(intent_id) REFERENCES intents(id),
                FOREIGN KEY(plan_id) REFERENCES plans(id)
            );

            CREATE TABLE IF NOT EXISTS factory_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factory_run_id INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                role TEXT,
                agent_run_id INTEGER,
                output_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                FOREIGN KEY(factory_run_id) REFERENCES factory_runs(id),
                FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id),
                UNIQUE(factory_run_id, stage_name)
            );

            CREATE TABLE IF NOT EXISTS factory_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factory_run_id INTEGER NOT NULL,
                artifact_type TEXT NOT NULL,
                artifact_id INTEGER NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(factory_run_id) REFERENCES factory_runs(id),
                UNIQUE(factory_run_id, artifact_type, artifact_id)
            );

            CREATE TABLE IF NOT EXISTS factory_policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL DEFAULT 'global',
                scope_id TEXT NOT NULL DEFAULT '',
                connector TEXT NOT NULL DEFAULT '',
                action_type TEXT NOT NULL DEFAULT '',
                allowed_mode TEXT NOT NULL DEFAULT 'review_first',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scope_type, scope_id, connector, action_type)
            );

            CREATE TABLE IF NOT EXISTS factory_learning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factory_run_id INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                notes TEXT,
                retrospective_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(factory_run_id) REFERENCES factory_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_factory_runs_intent ON factory_runs(intent_id, status, started_at);
            CREATE INDEX IF NOT EXISTS idx_factory_stages_run ON factory_stages(factory_run_id, stage_name, status);
            CREATE INDEX IF NOT EXISTS idx_factory_artifacts_run ON factory_artifacts(factory_run_id, artifact_type);
            CREATE INDEX IF NOT EXISTS idx_factory_policies_scope ON factory_policies(scope_type, scope_id, status);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (29, "add_ai_factory_workflow"),
        )

    if current < 30:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS route_eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_path TEXT NOT NULL,
                total_cases INTEGER NOT NULL,
                passed_cases INTEGER NOT NULL,
                accuracy REAL NOT NULL,
                low_confidence_cases INTEGER NOT NULL DEFAULT 0,
                model_shadow INTEGER NOT NULL DEFAULT 0,
                model_overrides INTEGER NOT NULL DEFAULT 0,
                calibration TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS route_eval_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_eval_run_id INTEGER NOT NULL,
                fixture_id TEXT NOT NULL,
                category TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                expected_intent TEXT NOT NULL,
                actual_intent TEXT NOT NULL,
                backend TEXT NOT NULL,
                confidence REAL NOT NULL,
                passed INTEGER NOT NULL DEFAULT 0,
                shadow_intent TEXT,
                shadow_backend TEXT,
                shadow_passed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(route_eval_run_id) REFERENCES route_eval_runs(id)
            );

            CREATE TABLE IF NOT EXISTS route_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_log_id INTEGER NOT NULL,
                surface TEXT,
                expected_intent TEXT NOT NULL,
                actual_intent TEXT NOT NULL,
                backend TEXT,
                confidence REAL NOT NULL DEFAULT 0.0,
                note_hash TEXT,
                note_length INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(event_log_id) REFERENCES event_log(id)
            );

            CREATE INDEX IF NOT EXISTS idx_route_eval_runs_created ON route_eval_runs(created_at);
            CREATE INDEX IF NOT EXISTS idx_route_eval_cases_run ON route_eval_cases(route_eval_run_id, fixture_id);
            CREATE INDEX IF NOT EXISTS idx_route_feedback_event ON route_feedback(event_log_id, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (30, "add_router_quality_loop"),
        )

    if current < 31:
        columns = conn.execute("PRAGMA table_info(route_feedback)").fetchall()
        names = {row["name"] for row in columns}
        if "text_hash" not in names:
            conn.execute("ALTER TABLE route_feedback ADD COLUMN text_hash TEXT")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS route_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text_hash TEXT NOT NULL UNIQUE,
                expected_intent TEXT NOT NULL,
                source_feedback_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(source_feedback_id) REFERENCES route_feedback(id)
            );

            CREATE INDEX IF NOT EXISTS idx_route_feedback_hash ON route_feedback(text_hash, created_at);
            CREATE INDEX IF NOT EXISTS idx_route_overrides_hash ON route_overrides(text_hash, status);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (31, "add_router_feedback_overrides"),
        )

    if current < 32:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS execution_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL UNIQUE,
                parent_correlation_id TEXT,
                surface TEXT NOT NULL DEFAULT 'cli',
                command TEXT NOT NULL,
                command_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                intent TEXT,
                command_tier TEXT,
                safety_level TEXT,
                route_event_id INTEGER,
                factory_run_id INTEGER,
                agent_task_id INTEGER,
                receipt_id INTEGER,
                summary TEXT,
                summary_hash TEXT,
                argv_hash TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(route_event_id) REFERENCES event_log(id),
                FOREIGN KEY(factory_run_id) REFERENCES factory_runs(id),
                FOREIGN KEY(agent_task_id) REFERENCES agent_tasks(id),
                FOREIGN KEY(receipt_id) REFERENCES action_execution_receipts(id)
            );

            CREATE TABLE IF NOT EXISTS execution_trace_rollups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_date TEXT NOT NULL,
                command_path TEXT NOT NULL,
                status TEXT NOT NULL,
                trace_count INTEGER NOT NULL DEFAULT 0,
                total_duration_ms INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(bucket_date, command_path, status)
            );

            CREATE INDEX IF NOT EXISTS idx_execution_traces_correlation ON execution_traces(correlation_id);
            CREATE INDEX IF NOT EXISTS idx_execution_traces_command ON execution_traces(command_path, status, started_at);
            CREATE INDEX IF NOT EXISTS idx_execution_traces_links ON execution_traces(route_event_id, factory_run_id, agent_task_id, receipt_id);
            CREATE INDEX IF NOT EXISTS idx_execution_rollups_bucket ON execution_trace_rollups(bucket_date, command_path);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (32, "add_lightweight_observability"),
        )

    if current < 33:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS autonomy_eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_cases INTEGER NOT NULL,
                passed_cases INTEGER NOT NULL,
                accuracy REAL NOT NULL,
                calibration TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS autonomy_eval_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                autonomy_eval_run_id INTEGER NOT NULL,
                fixture_id TEXT NOT NULL,
                command TEXT NOT NULL,
                safety TEXT NOT NULL,
                expected_decision TEXT NOT NULL,
                actual_decision TEXT NOT NULL,
                tier TEXT NOT NULL,
                passed INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(autonomy_eval_run_id) REFERENCES autonomy_eval_runs(id)
            );

            CREATE TABLE IF NOT EXISTS autonomy_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_trace_id INTEGER,
                command_path TEXT NOT NULL,
                safety_level TEXT,
                expected_decision TEXT NOT NULL,
                actual_decision TEXT NOT NULL,
                note_hash TEXT,
                note_length INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(execution_trace_id) REFERENCES execution_traces(id)
            );

            CREATE INDEX IF NOT EXISTS idx_autonomy_eval_runs_created ON autonomy_eval_runs(created_at);
            CREATE INDEX IF NOT EXISTS idx_autonomy_eval_cases_run ON autonomy_eval_cases(autonomy_eval_run_id, fixture_id);
            CREATE INDEX IF NOT EXISTS idx_autonomy_feedback_trace ON autonomy_feedback(execution_trace_id, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (33, "add_autonomy_decision_calibration"),
        )

    if current < 34:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS autonomy_run_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_type TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                assistant_goal_id INTEGER,
                agent_task_id INTEGER,
                agent_run_id INTEGER,
                correlation_id TEXT,
                provider TEXT,
                actions_proposed INTEGER NOT NULL DEFAULT 0,
                safe_actions_executed INTEGER NOT NULL DEFAULT 0,
                pending_approvals INTEGER NOT NULL DEFAULT 0,
                blocked_or_failed INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(assistant_goal_id) REFERENCES assistant_goals(id),
                FOREIGN KEY(agent_task_id) REFERENCES agent_tasks(id),
                FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_autonomy_run_ledger_created ON autonomy_run_ledger(created_at);
            CREATE INDEX IF NOT EXISTS idx_autonomy_run_ledger_goal ON autonomy_run_ledger(assistant_goal_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_autonomy_run_ledger_task ON autonomy_run_ledger(agent_task_id, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (34, "add_autonomy_run_ledger"),
        )

    if current < 35:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recommendation_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_key TEXT NOT NULL,
                label TEXT NOT NULL,
                command TEXT,
                decision TEXT,
                intent TEXT,
                workflow_pack TEXT,
                useful INTEGER NOT NULL,
                note_hash TEXT,
                note_length INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_key ON recommendation_feedback(recommendation_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_useful ON recommendation_feedback(useful, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (35, "add_recommendation_feedback"),
        )

    if current < 36:
        columns = conn.execute("PRAGMA table_info(factory_runs)").fetchall()
        names = {row["name"] for row in columns}
        if "executor_backend" not in names:
            conn.execute("ALTER TABLE factory_runs ADD COLUMN executor_backend TEXT NOT NULL DEFAULT 'local'")
        if "executor_context_json" not in names:
            conn.execute("ALTER TABLE factory_runs ADD COLUMN executor_context_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (36, "add_factory_executor_backend"),
        )

    if current < 37:
        # Approval integrity binding: pin a content hash of the payload and the
        # approval timestamp at the moment an action is approved, so tampering
        # or long-stale approvals are refused at execute time. Nullable columns
        # keep pre-existing rows executable (skip-when-null); rows approved via
        # `approve_and_execute` after this migration are always verified.
        columns = conn.execute("PRAGMA table_info(agent_actions)").fetchall()
        names = {row["name"] for row in columns}
        if "payload_hash" not in names:
            conn.execute("ALTER TABLE agent_actions ADD COLUMN payload_hash TEXT")
        if "approved_at" not in names:
            conn.execute("ALTER TABLE agent_actions ADD COLUMN approved_at TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (37, "add_approval_integrity_binding"),
        )

    _ensure_fts5(conn)  # self-heal: build the FTS index if a no-FTS5 run stranded migration 17
    conn.commit()


def _ensure_fts5(conn: sqlite3.Connection) -> None:
    """Idempotently create the FTS5 index + triggers if FTS5 is available and the
    table is missing. Runs every connect, independent of the migration version chain,
    so a DB first opened on a no-FTS5 build self-heals once opened on an FTS5 build
    (B2). No-op when FTS5 is unavailable."""
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_chunks_fts'").fetchone():
            return
        conn.execute(
            "CREATE VIRTUAL TABLE text_chunks_fts USING fts5(content, source_type UNINDEXED, source_id UNINDEXED)"
        )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS text_chunks_fts_ai AFTER INSERT ON text_chunks BEGIN
                INSERT INTO text_chunks_fts(rowid, content, source_type, source_id)
                VALUES (new.id, new.content, new.source_type, new.source_id);
            END;
            CREATE TRIGGER IF NOT EXISTS text_chunks_fts_ad AFTER DELETE ON text_chunks BEGIN
                DELETE FROM text_chunks_fts WHERE rowid = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS text_chunks_fts_au AFTER UPDATE ON text_chunks BEGIN
                UPDATE text_chunks_fts
                   SET content = new.content, source_type = new.source_type, source_id = new.source_id
                 WHERE rowid = new.id;
            END;
            """
        )
        conn.execute(
            "INSERT INTO text_chunks_fts(rowid, content, source_type, source_id) "
            "SELECT id, content, source_type, source_id FROM text_chunks "
            "WHERE id NOT IN (SELECT rowid FROM text_chunks_fts)"
        )
        # Record the FTS migrations we just satisfied so the ledger isn't left
        # claiming 17/19 unapplied while the objects exist (review B-1/B-2). This
        # also stops migration 19 from redundantly re-running on the next connect.
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (17, 'add_text_chunks_fts5')")
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (19, 'fix_fts_au_trigger')")
    except sqlite3.OperationalError:
        pass  # FTS5 not compiled in — retrieval falls back to the brute-force scan


def verify_schema(conn: sqlite3.Connection) -> dict[str, object]:
    """Return a compact readiness report for migration and storage health."""
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC").fetchall()
    applied = {int(row["version"]) for row in rows}
    required_tables = {
        "schema_migrations",
        "inbox_items",
        "work_items",
        "text_chunks",
        "knowledge_nodes",
        "knowledge_edges",
        "intents",
        "entities",
        "relationships",
        "retrieval_runs",
        "retrieval_run_sources",
        "plans",
        "plan_steps",
        "plan_risks",
        "plan_validations",
        "review_packets",
        "claims",
        "action_execution_receipts",
        "factory_runs",
        "factory_stages",
        "factory_artifacts",
        "factory_policies",
        "factory_learning",
        "execution_traces",
        "execution_trace_rollups",
        "autonomy_eval_runs",
        "autonomy_eval_cases",
        "autonomy_feedback",
    }
    existing_tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')").fetchall()
    }
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    missing_versions = [version for version in range(1, EXPECTED_SCHEMA_VERSION + 1) if version not in applied]
    missing_tables = sorted(required_tables - existing_tables)
    ok = (
        not missing_versions
        and not missing_tables
        and quick_check == "ok"
        and not foreign_keys
        and (max(applied) if applied else 0) >= EXPECTED_SCHEMA_VERSION
    )
    return {
        "ok": ok,
        "expected_version": EXPECTED_SCHEMA_VERSION,
        "current_version": max(applied) if applied else 0,
        "missing_versions": missing_versions,
        "missing_tables": missing_tables,
        "quick_check": quick_check,
        "foreign_key_violations": len(foreign_keys),
    }


def append_event(
    conn: sqlite3.Connection, event_type: str, entity_type: str, entity_id: int | None, payload: str
) -> None:
    conn.execute(
        """
        INSERT INTO event_log (event_type, entity_type, entity_id, payload)
        VALUES (?, ?, ?, ?)
        """,
        (event_type, entity_type, entity_id, payload),
    )
