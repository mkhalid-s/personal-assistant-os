"""Tests for connector pagination methods added in base.py.

All tests use a mock that intercepts json_get() — no real HTTP calls.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from personal_assistant.connectors.base import BaseConnector
from personal_assistant.db import initialize_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class _TestConnector(BaseConnector):
    """Minimal concrete subclass for testing base pagination."""
    name = "test"

    def fetch_items(self):  # type: ignore[override]
        return []


def _make_connector(env_overrides: dict | None = None) -> _TestConnector:
    conn = _conn()
    c = _TestConnector(conn)
    return c


# ---------------------------------------------------------------------------
# json_get_offset (Jira-style)
# ---------------------------------------------------------------------------

class JsonGetOffsetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = _make_connector()

    def _mock_offset(self, pages: list[list[dict]], total: int):
        """Returns a side_effect fn that serves pages in order."""
        calls = [0]
        def side_effect(url, headers):  # noqa: ANN001
            page = calls[0]
            calls[0] += 1
            return {"issues": pages[page] if page < len(pages) else [], "total": total}
        return side_effect

    def test_single_page_when_total_fits(self) -> None:
        items = [{"id": i} for i in range(3)]
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "50"}), \
             patch.object(self.c, "json_get", side_effect=self._mock_offset([items], 3)):
            result = self.c.json_get_offset("http://x/search", {}, result_key="issues")
        self.assertEqual(len(result), 3)

    def test_two_pages_when_total_exceeds_size(self) -> None:
        p1 = [{"id": i} for i in range(5)]
        p2 = [{"id": i} for i in range(5, 8)]
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "5"}), \
             patch.object(self.c, "json_get", side_effect=self._mock_offset([p1, p2], 8)):
            result = self.c.json_get_offset("http://x/search", {}, result_key="issues")
        self.assertEqual(len(result), 8)
        self.assertEqual([r["id"] for r in result], list(range(8)))

    def test_stops_at_max_pages(self) -> None:
        # 3 pages each with 2 items, but max_pages=2 → only 4 items fetched.
        page = [{"id": 0}, {"id": 1}]
        with patch.dict("os.environ", {
            "MYOS_CONNECTOR_PAGE_SIZE": "2",
            "MYOS_CONNECTOR_MAX_PAGES": "2",
        }), patch.object(self.c, "json_get", side_effect=self._mock_offset([page, page, page], 6)):
            result = self.c.json_get_offset("http://x/search", {}, result_key="issues")
        self.assertEqual(len(result), 4)

    def test_empty_first_page_returns_empty(self) -> None:
        with patch.object(self.c, "json_get", return_value={"issues": [], "total": 0}):
            result = self.c.json_get_offset("http://x/search", {}, result_key="issues")
        self.assertEqual(result, [])

    def test_offset_appended_to_url(self) -> None:
        seen_urls: list[str] = []
        def capture(url, headers):  # noqa: ANN001
            seen_urls.append(url)
            return {"issues": [{"id": 0}, {"id": 1}], "total": 10}
        # After first page (2 items out of 10), will try second page
        call_count = [0]
        def limited_capture(url, headers):  # noqa: ANN001
            call_count[0] += 1
            seen_urls.append(url)
            if call_count[0] == 1:
                return {"issues": [{"id": 0}, {"id": 1}], "total": 4}
            return {"issues": [{"id": 2}, {"id": 3}], "total": 4}
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "2"}), \
             patch.object(self.c, "json_get", side_effect=limited_capture):
            self.c.json_get_offset("http://x/search?q=1", {}, result_key="issues")
        self.assertIn("startAt=0", seen_urls[0])
        self.assertIn("startAt=2", seen_urls[1])
        self.assertIn("maxResults=2", seen_urls[0])


# ---------------------------------------------------------------------------
# json_get_paged (GitHub / Aha-style)
# ---------------------------------------------------------------------------

class JsonGetPagedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = _make_connector()

    def test_bare_list_response(self) -> None:
        # GitHub: response is a bare list, result_key=""
        pages = [[{"n": i} for i in range(5)], [{"n": 5}, {"n": 6}]]
        calls = [0]
        def side_effect(url, headers):  # noqa: ANN001
            page = calls[0]; calls[0] += 1
            return pages[page] if page < len(pages) else []
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "5"}), \
             patch.object(self.c, "json_get", side_effect=side_effect):
            result = self.c.json_get_paged("http://x/prs", {}, result_key="")
        self.assertEqual(len(result), 7)

    def test_dict_response_with_key(self) -> None:
        # Aha: response is {"features": [...]}
        p1 = {"features": [{"id": i} for i in range(3)]}
        p2 = {"features": [{"id": 3}]}
        calls = [0]
        def side_effect(url, headers):  # noqa: ANN001
            page = calls[0]; calls[0] += 1
            return [p1, p2][page] if page < 2 else {"features": []}
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "3"}), \
             patch.object(self.c, "json_get", side_effect=side_effect):
            result = self.c.json_get_paged("http://x/features", {}, result_key="features")
        self.assertEqual(len(result), 4)

    def test_stops_on_short_last_page(self) -> None:
        # Full page then partial → stop
        p1 = [{"id": i} for i in range(5)]
        p2 = [{"id": 5}]
        calls = [0]
        def side_effect(url, headers):  # noqa: ANN001
            page = calls[0]; calls[0] += 1
            return [p1, p2][page] if page < 2 else []
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "5"}), \
             patch.object(self.c, "json_get", side_effect=side_effect):
            result = self.c.json_get_paged("http://x/prs", {}, result_key="")
        self.assertEqual(len(result), 6)
        self.assertEqual(calls[0], 2)

    def test_stops_on_empty_page(self) -> None:
        # When page_size == len(first page), the loop fetches page 2 and stops
        # on empty. When len < page_size the loop stops after page 1 without probing.
        # This test uses page_size == len(first page) to verify the empty-stop path.
        p1 = [{"id": 0}, {"id": 1}]
        calls = [0]
        def side_effect(url, headers):  # noqa: ANN001
            page = calls[0]; calls[0] += 1
            return p1 if page == 0 else []
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "2"}), \
             patch.object(self.c, "json_get", side_effect=side_effect):
            result = self.c.json_get_paged("http://x/prs", {}, result_key="")
        self.assertEqual(len(result), 2)
        self.assertEqual(calls[0], 2)  # first full page + empty probe page

    def test_page_param_appended(self) -> None:
        seen: list[str] = []
        def capture(url, headers):  # noqa: ANN001
            seen.append(url)
            return [] if len(seen) > 1 else [{"id": 0}]
        with patch.dict("os.environ", {"MYOS_CONNECTOR_PAGE_SIZE": "50"}), \
             patch.object(self.c, "json_get", side_effect=capture):
            self.c.json_get_paged("http://x/prs?state=open", {}, result_key="")
        self.assertIn("page=1", seen[0])
        self.assertIn("per_page=50", seen[0])


# ---------------------------------------------------------------------------
# json_get_linked (Confluence-style)
# ---------------------------------------------------------------------------

class JsonGetLinkedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = _make_connector()

    def test_follows_next_link(self) -> None:
        responses = {
            "http://base/wiki/search": {
                "results": [{"id": 0}, {"id": 1}],
                "_links": {"next": "/wiki/search?start=2"},
            },
            "http://base/wiki/search?start=2": {
                "results": [{"id": 2}],
                "_links": {},
            },
        }
        with patch.object(self.c, "json_get", side_effect=lambda u, h: responses[u]):
            result = self.c.json_get_linked(
                "http://base/wiki/search", {},
                result_key="results",
                next_path="_links.next",
                base_url="http://base",
            )
        self.assertEqual(len(result), 3)
        self.assertEqual([r["id"] for r in result], [0, 1, 2])

    def test_stops_when_no_next(self) -> None:
        data = {"results": [{"id": 0}], "_links": {}}
        with patch.object(self.c, "json_get", return_value=data):
            result = self.c.json_get_linked(
                "http://x", {}, result_key="results",
                next_path="_links.next", base_url="http://x",
            )
        self.assertEqual(len(result), 1)

    def test_absolute_next_url(self) -> None:
        # When next is a full URL (not a path), use it directly
        responses = [
            {"results": [{"id": 0}], "_links": {"next": "http://other/wiki/search?start=1"}},
            {"results": [{"id": 1}], "_links": {}},
        ]
        calls = [0]
        def side_effect(url, headers):  # noqa: ANN001
            r = responses[calls[0]]; calls[0] += 1; return r
        with patch.object(self.c, "json_get", side_effect=side_effect):
            result = self.c.json_get_linked(
                "http://base/wiki", {}, result_key="results",
                next_path="_links.next", base_url="http://base",
            )
        self.assertEqual(len(result), 2)

    def test_max_pages_stops_runaway(self) -> None:
        # Always returns a next link — should stop at max_pages.
        data = {"results": [{"id": 0}], "_links": {"next": "/wiki/search?start=1"}}
        with patch.dict("os.environ", {"MYOS_CONNECTOR_MAX_PAGES": "3"}), \
             patch.object(self.c, "json_get", return_value=data):
            result = self.c.json_get_linked(
                "http://x", {}, result_key="results",
                next_path="_links.next", base_url="http://x",
            )
        self.assertEqual(len(result), 3)


if __name__ == "__main__":
    unittest.main()
