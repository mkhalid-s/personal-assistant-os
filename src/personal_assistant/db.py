from __future__ import annotations

import sqlite3
import os
from pathlib import Path

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

    current = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()["version"]

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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_commitment_outcome ON commitment_log(outcome, due_on)"
        )
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

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_dedupe ON inbox_items(text, kind, source)"
        )
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

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_inbox_unique ON work_items(inbox_id)"
        )
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_steps_run ON workflow_steps(workflow_run_id, status)"
        )
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assistant_goals_status ON assistant_goals(status, last_evaluated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_action_provider_executions_action ON action_provider_executions(agent_action_id, created_at)")
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
        try:
            conn.execute("ALTER TABLE review_evidence ADD COLUMN person_id INTEGER")
        except sqlite3.OperationalError:
            pass  # column already added on a prior run
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
        has_fts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_chunks_fts'"
        ).fetchone()
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

    _ensure_fts5(conn)  # self-heal: build the FTS index if a no-FTS5 run stranded migration 17
    conn.commit()


def _ensure_fts5(conn: sqlite3.Connection) -> None:
    """Idempotently create the FTS5 index + triggers if FTS5 is available and the
    table is missing. Runs every connect, independent of the migration version chain,
    so a DB first opened on a no-FTS5 build self-heals once opened on an FTS5 build
    (B2). No-op when FTS5 is unavailable."""
    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_chunks_fts'"
        ).fetchone():
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
