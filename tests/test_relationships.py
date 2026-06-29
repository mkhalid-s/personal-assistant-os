from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from personal_assistant import relationships
from personal_assistant.db import initialize_schema


class RelationshipExtractionTest(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        return conn

    def test_extract_relationships_is_typed_and_deterministic(self) -> None:
        text = "Project Atlas depends on service Billing API. PR #42 references PAOS-123."
        first = relationships.extract_relationships(text)
        second = relationships.extract_relationships(text)

        self.assertEqual(first, second)
        edges = {
            (
                rel["from_entity"]["canonical_name"],
                rel["relation_type"],
                rel["to_entity"]["canonical_name"],
            )
            for rel in first
        }
        self.assertIn(("Project Atlas", "depends_on", "Service Billing API"), edges)
        self.assertIn(("PR #42", "references", "PAOS-123"), edges)

    def test_extract_relationships_handles_inverse_phrase(self) -> None:
        found = relationships.extract_relationships("Project Atlas is blocked by service Billing API.")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["from_entity"]["canonical_name"], "Service Billing API")
        self.assertEqual(found[0]["relation_type"], "blocks")
        self.assertEqual(found[0]["to_entity"]["canonical_name"], "Project Atlas")

    def test_record_relationships_persists_entities_and_deduplicates(self) -> None:
        conn = self._conn()
        try:
            recorded = relationships.record_relationships(
                conn,
                "Project Atlas depends on service Billing API.",
                source_type="note",
                source_id="rel-1",
            )
            conn.commit()
            repeated = relationships.record_relationships(
                conn,
                "Project Atlas depends on service Billing API.",
                source_type="note",
                source_id="rel-1",
            )
            conn.commit()

            self.assertEqual(len(recorded), 1)
            self.assertEqual(repeated[0]["id"], recorded[0]["id"])
            rows = relationships.list_relationships(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["from_name"], "Project Atlas")
            self.assertEqual(rows[0]["relation_type"], "depends_on")
            self.assertEqual(rows[0]["to_name"], "Service Billing API")
            self.assertEqual(rows[0]["source_type"], "note")
            self.assertEqual(rows[0]["source_id"], "rel-1")

            relationships.record_relationships(conn, "Project Atlas depends on service Billing API.")
            relationships.record_relationships(conn, "Project Atlas depends on service Billing API.")
            conn.commit()
            self.assertEqual(len(relationships.list_relationships(conn)), 2)
        finally:
            conn.close()

    def test_record_relationships_applies_privacy_filters(self) -> None:
        conn = self._conn()
        try:
            recorded = relationships.record_relationships(
                conn,
                "Email test@example.com says Project Atlas depends on service Billing API with token ghp_abcdefghijklmnopqrstuvwxyz123456.",
                source_type="note",
                source_id="privacy",
            )
            conn.commit()

            self.assertEqual(len(recorded), 1)
            evidence = conn.execute("SELECT evidence FROM relationships").fetchone()["evidence"]
            self.assertIn("[REDACTED_EMAIL]", evidence)
            self.assertIn("[REDACTED_SECRET]", evidence)
            self.assertNotIn("test@example.com", evidence)
            self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", evidence)
        finally:
            conn.close()

    def test_relationship_cli_extract_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["MYOS_DB_PATH"] = str(Path(tmp) / "assistant.db")
            base_cmd = ["python", "-m", "personal_assistant.cli"]

            out = subprocess.run(
                base_cmd
                + [
                    "relationship",
                    "extract",
                    "--text",
                    "Project Atlas depends on service Billing API.",
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
            self.assertIn("Recorded 1 relationships", out.stdout)
            self.assertIn("Project Atlas -[depends_on]-> Service Billing API", out.stdout)

            listed = subprocess.run(
                base_cmd + ["relationship", "list", "--type", "depends_on"],
                cwd=Path.cwd(),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Relationships:", listed.stdout)
            self.assertIn("Project Atlas -[depends_on]-> Service Billing API", listed.stdout)
            self.assertIn("source=note:cli", listed.stdout)


if __name__ == "__main__":
    unittest.main()
