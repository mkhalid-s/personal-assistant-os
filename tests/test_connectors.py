from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
