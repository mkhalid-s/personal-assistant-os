from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path

from personal_assistant.connectors.base import BaseConnector
from personal_assistant.db import initialize_schema
from personal_assistant.models import ExternalItem


class DummyConnector(BaseConnector):
    name = "dummy"

    def fetch_items(self):
        return [
            ExternalItem(
                connector=self.name,
                external_id="1",
                item_type="issue",
                title="Dummy item",
                body="Body",
                status="open",
            )
        ]


class ConnectorTest(unittest.TestCase):
    def test_connector_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "assistant.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            initialize_schema(conn)
            result = DummyConnector(conn).sync()
            self.assertEqual(result.status, "ok")
            count = conn.execute("SELECT COUNT(*) AS c FROM external_items").fetchone()["c"]
            self.assertEqual(count, 1)
            conn.close()

    def test_connector_redacts_fields_and_raw_payload_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "assistant.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            initialize_schema(conn)
            connector = DummyConnector(conn)
            connector.upsert_external(
                ExternalItem(
                    connector="dummy",
                    external_id="private-1",
                    item_type="issue",
                    title="Contact alice@example.com",
                    body="token=supersecretvalue",
                    owner="alice@example.com",
                    status="open",
                    url="https://example.test/item?token=supersecretvalue",
                    raw={"reporter": "alice@example.com", "api_token": "supersecretvalue"},
                )
            )
            row = conn.execute("SELECT * FROM external_items WHERE external_id='private-1'").fetchone()
            self.assertNotIn("alice@example.com", row["title"])
            self.assertNotIn("supersecretvalue", row["body"])
            self.assertNotIn("alice@example.com", row["owner"])
            self.assertNotIn("supersecretvalue", row["url"])
            raw = json.loads(row["raw_json"])
            self.assertNotIn("alice@example.com", raw["reporter"])
            self.assertNotIn("supersecretvalue", raw["api_token"])
            conn.close()

    def test_json_get_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "assistant.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            initialize_schema(conn)
            connector = DummyConnector(conn)

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return b'{"ok": true}'

            calls = {"count": 0}
            original = urllib.request.urlopen

            def fake_urlopen(req, timeout=25):
                calls["count"] += 1
                if calls["count"] < 3:
                    raise RuntimeError("transient")
                return FakeResponse()

            urllib.request.urlopen = fake_urlopen
            os.environ["MYOS_CONNECTOR_RETRIES"] = "3"
            os.environ["MYOS_CONNECTOR_BACKOFF_SEC"] = "0"
            try:
                result = connector.json_get("https://example.test", {})
                self.assertTrue(result["ok"])
                self.assertEqual(calls["count"], 3)
            finally:
                urllib.request.urlopen = original
                os.environ.pop("MYOS_CONNECTOR_RETRIES", None)
                os.environ.pop("MYOS_CONNECTOR_BACKOFF_SEC", None)
                conn.close()

    def test_migration_scrubs_preexisting_connector_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "assistant.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            initialize_schema(conn)
            conn.execute(
                """
                INSERT INTO external_items (
                    connector, external_id, item_type, title, body, owner, raw_json
                ) VALUES ('dummy', 'legacy', 'issue', ?, ?, ?, ?)
                """,
                (
                    "Legacy alice@example.com",
                    "token=supersecretvalue",
                    "alice@example.com",
                    json.dumps({"reporter": "alice@example.com", "api_token": "supersecretvalue"}),
                ),
            )
            conn.execute("DELETE FROM schema_migrations WHERE version=41")
            conn.commit()
            initialize_schema(conn)
            row = conn.execute("SELECT * FROM external_items WHERE external_id='legacy'").fetchone()
            self.assertNotIn("alice@example.com", row["title"])
            self.assertNotIn("supersecretvalue", row["body"])
            raw = json.loads(row["raw_json"])
            self.assertEqual(raw["api_token"], "[REDACTED_SECRET]")
            version = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"]
            self.assertEqual(version, 41)
            conn.close()


if __name__ == "__main__":
    unittest.main()
