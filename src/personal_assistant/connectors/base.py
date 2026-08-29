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
from personal_assistant.privacy import apply_privacy_filters, redact_obj


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
        def safe(value: str | None) -> str | None:
            return apply_privacy_filters(self.conn, value) if value is not None else None

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
                safe(item.title),
                safe(item.body),
                safe(item.owner),
                safe(item.status),
                safe(item.priority),
                safe(item.due_date),
                safe(item.url),
                json.dumps(redact_obj(self.conn, item.raw or {}), ensure_ascii=True),
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

    def _page_size(self) -> int:
        return int(os.getenv("MYOS_CONNECTOR_PAGE_SIZE", "50"))

    def _max_pages(self) -> int:
        return int(os.getenv("MYOS_CONNECTOR_MAX_PAGES", "20"))

    def json_get_offset(
        self,
        url: str,
        headers: dict[str, str],
        *,
        result_key: str,
        offset_param: str = "startAt",
        size_param: str = "maxResults",
        total_key: str = "total",
    ) -> list[Any]:
        """Offset-based pagination (Jira-style).

        Reads ``total`` from the first response and loops, incrementing the
        offset by page_size, until all items are fetched or max_pages is hit.
        """
        page_size = self._page_size()
        sep = "&" if "?" in url else "?"
        all_items: list[Any] = []
        for page in range(self._max_pages()):
            offset = page * page_size
            paged = f"{url}{sep}{offset_param}={offset}&{size_param}={page_size}"
            data = self.json_get(paged, headers)
            items = data.get(result_key, []) if isinstance(data, dict) else []
            if not isinstance(items, list) or not items:
                break
            all_items.extend(items)
            total = int(data.get(total_key, 0)) if isinstance(data, dict) else 0
            if offset + page_size >= total:
                break
        return all_items

    def json_get_paged(
        self,
        url: str,
        headers: dict[str, str],
        *,
        result_key: str = "",
        page_param: str = "page",
        size_param: str = "per_page",
    ) -> list[Any]:
        """Page-number pagination (GitHub / Aha-style).

        Starts at page 1 and increments until a page returns fewer items than
        page_size (indicating the last page) or an empty list. The response may
        be a bare list (GitHub) or a dict with a result_key (Aha).
        """
        page_size = self._page_size()
        sep = "&" if "?" in url else "?"
        all_items: list[Any] = []
        for page in range(1, self._max_pages() + 1):
            paged = f"{url}{sep}{page_param}={page}&{size_param}={page_size}"
            data = self.json_get(paged, headers)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and result_key:
                items = data.get(result_key, [])
            else:
                break
            if not isinstance(items, list) or not items:
                break
            all_items.extend(items)
            if len(items) < page_size:
                break
        return all_items

    def json_get_linked(
        self,
        url: str,
        headers: dict[str, str],
        *,
        result_key: str,
        next_path: str = "_links.next",
        base_url: str = "",
    ) -> list[Any]:
        """Link-following pagination (Confluence-style).

        Reads ``next_path`` (dot-separated key path into the response JSON)
        to find the next page URL. Stops when the key is absent or empty.
        """
        all_items: list[Any] = []
        current = url
        for _ in range(self._max_pages()):
            data = self.json_get(current, headers)
            items = data.get(result_key, []) if isinstance(data, dict) else []
            if not isinstance(items, list):
                break
            if not items:
                break
            all_items.extend(items)
            # Walk the dot-separated key path to find the next URL.
            node: Any = data
            for part in next_path.split("."):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            next_href = node if isinstance(node, str) else ""
            if not next_href:
                break
            current = (base_url + next_href) if next_href.startswith("/") else next_href
        return all_items

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
