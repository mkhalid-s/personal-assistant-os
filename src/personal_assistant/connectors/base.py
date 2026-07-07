from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from personal_assistant.models import ExternalItem


@dataclass(slots=True)
class ConnectorResult:
    connector: str
    fetched: int
    status: str
    message: str = ""


class BaseConnector:
    name = "base"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def required_env(self) -> list[str]:
        return []

    def fetch_items(self) -> list[ExternalItem]:
        raise NotImplementedError

    def validate_env(self) -> tuple[bool, str]:
        for key in self.required_env():
            if not os.getenv(key):
                return False, f"missing env var: {key}"
        return True, "ok"

    def get_cursor(self) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM sync_state WHERE connector = ?",
            (self.name,),
        ).fetchone()
        return row["cursor"] if row else None

    def set_sync_state(self, cursor: str | None, status: str, error: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO sync_state (connector, cursor, last_success_at, last_status, last_error)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(connector) DO UPDATE SET
              cursor=excluded.cursor,
              last_success_at=CASE
                WHEN excluded.last_status = 'ok' THEN CURRENT_TIMESTAMP
                ELSE sync_state.last_success_at
              END,
              last_status=excluded.last_status,
              last_error=excluded.last_error
            """,
            (self.name, cursor, status, error),
        )

    def upsert_external(self, item: ExternalItem) -> None:
        self.conn.execute(
            """
            INSERT INTO external_items (
                connector, external_id, item_type, title, body, owner,
                status, priority, due_date, url, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connector, external_id, item_type) DO UPDATE SET
              title=excluded.title,
              body=excluded.body,
              owner=excluded.owner,
              status=excluded.status,
              priority=excluded.priority,
              due_date=excluded.due_date,
              url=excluded.url,
              raw_json=excluded.raw_json,
              fetched_at=CURRENT_TIMESTAMP
            """,
            (
                item.connector,
                item.external_id,
                item.item_type,
                item.title,
                item.body,
                item.owner,
                item.status,
                item.priority,
                item.due_date,
                item.url,
                json.dumps(item.raw or {}, ensure_ascii=True),
            ),
        )

    def json_get(self, url: str, headers: dict[str, str]) -> Any:
        retries = int(os.getenv("MYOS_CONNECTOR_RETRIES", "3"))
        backoff_sec = float(os.getenv("MYOS_CONNECTOR_BACKOFF_SEC", "1.2"))
        timeout_sec = int(os.getenv("MYOS_CONNECTOR_TIMEOUT_SEC", "25"))
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # pragma: no cover
                last_exc = exc
                if attempt == retries - 1:
                    break
                sleep_for = backoff_sec * (2**attempt)
                time.sleep(sleep_for)
        assert last_exc is not None
        raise last_exc

    def sync(self) -> ConnectorResult:
        ok, reason = self.validate_env()
        if not ok:
            self.set_sync_state(self.get_cursor(), "skipped", reason)
            self.conn.commit()
            return ConnectorResult(self.name, 0, "skipped", reason)

        try:
            items = self.fetch_items()
            for item in items:
                self.upsert_external(item)
            cursor = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.set_sync_state(cursor, "ok", "")
            self.conn.commit()
            return ConnectorResult(self.name, len(items), "ok")
        except Exception as exc:  # pragma: no cover
            self.set_sync_state(self.get_cursor(), "error", str(exc))
            self.conn.commit()
            return ConnectorResult(self.name, 0, "error", str(exc))
