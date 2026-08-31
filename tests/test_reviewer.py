"""Tests for reviewer.py — safety reviewer model escalation (A3)."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from personal_assistant.db import initialize_schema
from personal_assistant.reviewer import _VALID_VERDICTS, classify_action_safety, reviewer_backend_name


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class ReviewerBackendNameTest(unittest.TestCase):
    def test_empty_when_not_set(self) -> None:
        import os

        os.environ.pop("MYOS_AUTO_REVIEWER", None)
        self.assertEqual(reviewer_backend_name(), "")

    def test_returns_configured_name(self) -> None:
        with patch.dict("os.environ", {"MYOS_AUTO_REVIEWER": "claude-haiku"}):
            self.assertEqual(reviewer_backend_name(), "claude-haiku")

    def test_strips_and_lowercases(self) -> None:
        with patch.dict("os.environ", {"MYOS_AUTO_REVIEWER": "  Claude-Haiku  "}):
            self.assertEqual(reviewer_backend_name(), "claude-haiku")


class ClassifyActionSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _mock_backend(self, reply: str, available: bool = True) -> MagicMock:
        b = MagicMock()
        b.available.return_value = (available, "ok" if available else "not configured")
        b.reason.return_value = {"reply": reply}
        return b

    def test_allow_verdict(self) -> None:
        with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend("allow")):
            result = classify_action_safety(self.conn, "local_note", {}, "claude-haiku")
        self.assertEqual(result, "allow")

    def test_escalate_verdict(self) -> None:
        with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend("escalate")):
            result = classify_action_safety(self.conn, "apply_patch", {"patch": "..."}, "claude-haiku")
        self.assertEqual(result, "escalate")

    def test_block_verdict(self) -> None:
        with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend("block")):
            result = classify_action_safety(self.conn, "apply_patch", {}, "claude-haiku")
        self.assertEqual(result, "block")

    def test_fails_open_on_unavailable_backend(self) -> None:
        with patch(
            "personal_assistant.providers.get_backend", return_value=self._mock_backend("allow", available=False)
        ):
            result = classify_action_safety(self.conn, "local_note", {}, "claude-haiku")
        self.assertEqual(result, "allow")

    def test_fails_open_on_provider_exception(self) -> None:
        with patch("personal_assistant.providers.get_backend", side_effect=RuntimeError("crashed")):
            result = classify_action_safety(self.conn, "local_note", {}, "claude-haiku")
        self.assertEqual(result, "allow")

    def test_fails_open_on_unknown_reply(self) -> None:
        with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend("maybe")):
            result = classify_action_safety(self.conn, "local_note", {}, "claude-haiku")
        self.assertEqual(result, "allow")

    def test_strips_punctuation_from_reply(self) -> None:
        with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend("escalate.")):
            result = classify_action_safety(self.conn, "apply_patch", {}, "claude-haiku")
        self.assertEqual(result, "escalate")

    def test_all_valid_verdicts_recognized(self) -> None:
        for verdict in _VALID_VERDICTS:
            with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend(verdict)):
                result = classify_action_safety(self.conn, "local_note", {}, "claude-haiku")
            self.assertEqual(result, verdict)

    def test_empty_reply_fails_open(self) -> None:
        with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend("")):
            result = classify_action_safety(self.conn, "local_note", {}, "claude-haiku")
        self.assertEqual(result, "allow")

    def test_json_braces_in_payload_do_not_raise(self) -> None:
        # Regression: payload_summary contains { and } from JSON serialisation.
        # Using str.format() on the prompt caused KeyError; now uses concatenation.
        payload_with_braces = {"target": "jira", "body": "{some content with braces}"}
        with patch("personal_assistant.providers.get_backend", return_value=self._mock_backend("allow")):
            result = classify_action_safety(self.conn, "draft_external_update", payload_with_braces, "claude-haiku")
        self.assertEqual(result, "allow")


if __name__ == "__main__":
    unittest.main()
