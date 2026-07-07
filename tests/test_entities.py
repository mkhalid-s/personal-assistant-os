from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from personal_assistant import entities
from personal_assistant.db import initialize_schema


class EntityExtractionTest(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        return conn

    def test_extract_entities_is_conservative_and_deterministic(self) -> None:
        text = (
            "Project Atlas depends on service Billing API for PAOS-123 and PR #42. "
            "See mkhalid-s/personal-assistant-os and https://example.com/docs/runbook. "
            "Ask @Owner for review."
        )
        first = entities.extract_entities(text)
        second = entities.extract_entities(text)

        self.assertEqual(first, second)
        by_key = {(e["entity_type"], e["canonical_name"]): e for e in first}
        self.assertIn(("project", "Project Atlas"), by_key)
        self.assertIn(("service", "Service Billing API"), by_key)
        self.assertIn(("ticket", "PAOS-123"), by_key)
        self.assertIn(("pull_request", "PR #42"), by_key)
        self.assertIn(("repository", "mkhalid-s/personal-assistant-os"), by_key)
        self.assertIn(("document", "https://example.com/docs/runbook"), by_key)
        self.assertIn(("person", "@owner"), by_key)
        self.assertNotIn(("repository", "example.com/docs"), by_key)

    def test_record_entities_persists_aliases_and_deduplicates(self) -> None:
        conn = self._conn()
        try:
            recorded = entities.record_entities(
                conn,
                "Project Atlas references PAOS-123 and PAOS-123.",
                source_type="work_item",
                source_id=7,
            )
            conn.commit()

            self.assertEqual([e["canonical_name"] for e in recorded], ["PAOS-123", "Project Atlas"])
            repeated = entities.record_entities(conn, "PAOS-123 is still open.", source_type="work_item", source_id=8)
            conn.commit()
            self.assertEqual(repeated[0]["id"], recorded[0]["id"])

            rows = entities.list_entities(conn)
            self.assertEqual(len(rows), 2)
            tickets = entities.list_entities(conn, entity_type="ticket")
            self.assertEqual(len(tickets), 1)
            self.assertEqual(tickets[0]["canonical_name"], "PAOS-123")
            self.assertIn("PAOS-123", tickets[0]["aliases"])

            alias = conn.execute("SELECT source_type, source_id FROM entity_aliases WHERE alias='PAOS-123'").fetchone()
            self.assertEqual(alias["source_type"], "work_item")
            self.assertIn(alias["source_id"], {"7", "8"})
        finally:
            conn.close()

    def test_record_entities_applies_privacy_filters_before_persistence(self) -> None:
        conn = self._conn()
        try:
            recorded = entities.record_entities(
                conn,
                "Email test@example.com and use token ghp_abcdefghijklmnopqrstuvwxyz123456 for Project Atlas.",
                source_type="note",
                source_id="privacy",
            )
            conn.commit()

            self.assertEqual([e["canonical_name"] for e in recorded], ["Project Atlas"])
            persisted = "\n".join(
                row["canonical_name"] for row in conn.execute("SELECT canonical_name FROM entities").fetchall()
            )
            aliases = "\n".join(row["alias"] for row in conn.execute("SELECT alias FROM entity_aliases").fetchall())
            self.assertNotIn("test@example.com", persisted + aliases)
            self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", persisted + aliases)
        finally:
            conn.close()

    def test_entity_cli_extract_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            base_cmd = [sys.executable, "-m", "personal_assistant.cli"]

            out = subprocess.run(
                base_cmd
                + [
                    "entity",
                    "extract",
                    "--text",
                    "Service Search API owns PAOS-456. See PR #77.",
                    "--source-type",
                    "note",
                    "--source-id",
                    "cli",
                ],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Recorded", out.stdout)
            self.assertIn("[service] Service Search API", out.stdout)
            self.assertIn("[ticket] PAOS-456", out.stdout)

            listed = subprocess.run(
                base_cmd + ["entity", "list", "--type", "ticket"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Entities:", listed.stdout)
            self.assertIn("[ticket] PAOS-456", listed.stdout)
            self.assertNotIn("Service Search API", listed.stdout)


if __name__ == "__main__":
    unittest.main()
