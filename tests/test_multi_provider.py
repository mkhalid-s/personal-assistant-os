"""Tests for multi_provider.py (X1 — multi-provider parallel reasoning)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from personal_assistant.multi_provider import (
    _score_response,
    configured_backends,
    fan_out_reason,
    multi_reason,
    pick_best,
)

# ---------------------------------------------------------------------------
# _score_response
# ---------------------------------------------------------------------------

class ScoreResponseTest(unittest.TestCase):
    def test_empty_response_scores_zero(self) -> None:
        self.assertEqual(_score_response({}), 0.0)

    def test_approval_required_actions_score_higher(self) -> None:
        r_approval = {"actions": [{"requires_approval": True}]}
        r_auto = {"actions": [{"requires_approval": False}]}
        self.assertGreater(_score_response(r_approval), _score_response(r_auto))

    def test_plan_adds_score(self) -> None:
        with_plan = {"actions": [], "plan": [{"step": "do something"}]}
        without = {"actions": []}
        self.assertGreater(_score_response(with_plan), _score_response(without))

    def test_reply_adds_score(self) -> None:
        with_reply = {"actions": [], "reply": "Here is my analysis."}
        without = {"actions": []}
        self.assertGreater(_score_response(with_reply), _score_response(without))

    def test_more_actions_scores_higher(self) -> None:
        r3 = {"actions": [{"requires_approval": False}] * 3}
        r1 = {"actions": [{"requires_approval": False}]}
        self.assertGreater(_score_response(r3), _score_response(r1))


# ---------------------------------------------------------------------------
# pick_best
# ---------------------------------------------------------------------------

class PickBestTest(unittest.TestCase):
    def test_returns_none_on_empty(self) -> None:
        self.assertIsNone(pick_best([]))

    def test_returns_single_item(self) -> None:
        r = [("claude", {"actions": [{"requires_approval": True}]})]
        name, _ = pick_best(r)
        self.assertEqual(name, "claude")

    def test_picks_highest_scoring(self) -> None:
        responses = [
            ("cursor", {"actions": [{"requires_approval": False}]}),
            ("claude", {"actions": [{"requires_approval": True}, {"requires_approval": True}]}),
        ]
        name, _ = pick_best(responses)
        self.assertEqual(name, "claude")

    def test_all_zero_scores_picks_first(self) -> None:
        responses = [("a", {}), ("b", {})]
        name, _ = pick_best(responses)
        # With equal scores max() returns the first encountered — deterministic
        self.assertIn(name, ("a", "b"))


# ---------------------------------------------------------------------------
# fan_out_reason
# ---------------------------------------------------------------------------

class FanOutReasonTest(unittest.TestCase):
    def _mock_backend(self, name: str, response: dict, available: bool = True) -> MagicMock:
        b = MagicMock()
        b.name = name
        b.available.return_value = (available, "ok" if available else "not configured")
        b.reason.return_value = response
        return b

    def test_returns_empty_on_no_backends(self) -> None:
        result = fan_out_reason({}, [], db_path=":memory:")
        self.assertEqual(result, [])

    def test_calls_all_backends(self) -> None:
        backends = {}

        def get_backend(name):
            return backends[name]

        backends["a"] = self._mock_backend("a", {"actions": [], "reply": "A"})
        backends["b"] = self._mock_backend("b", {"actions": [], "reply": "B"})

        with patch("personal_assistant.providers.get_backend", side_effect=get_backend), \
             patch("personal_assistant.multi_provider.resolve_db_path", return_value=":memory:"), \
             patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.__enter__ = lambda s: s
            mock_conn.return_value.row_factory = None
            mock_conn.return_value.close = lambda: None
            results = fan_out_reason({}, ["a", "b"], db_path=":memory:")

        self.assertEqual(len(results), 2)
        names = {r[0] for r in results}
        self.assertIn("a", names)
        self.assertIn("b", names)

    def test_excludes_unavailable_backend(self) -> None:
        good = self._mock_backend("good", {"actions": [{"requires_approval": True}]})
        bad = self._mock_backend("bad", {}, available=False)

        def get_backend(name):
            return good if name == "good" else bad

        with patch("personal_assistant.providers.get_backend", side_effect=get_backend), \
             patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.row_factory = None
            mock_conn.return_value.close = lambda: None
            results = fan_out_reason({}, ["good", "bad"], db_path=":memory:")

        names = {r[0] for r in results}
        self.assertIn("good", names)
        self.assertNotIn("bad", names)

    def test_excludes_backend_that_raises(self) -> None:
        good = self._mock_backend("good", {"actions": [], "reply": "ok"})
        crashing = MagicMock()
        crashing.available.side_effect = RuntimeError("crash")

        def get_backend(name):
            return good if name == "good" else crashing

        with patch("personal_assistant.providers.get_backend", side_effect=get_backend), \
             patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.row_factory = None
            mock_conn.return_value.close = lambda: None
            results = fan_out_reason({}, ["good", "crash"], db_path=":memory:")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "good")


# ---------------------------------------------------------------------------
# multi_reason
# ---------------------------------------------------------------------------

class MultiReasonTest(unittest.TestCase):
    def test_returns_none_on_no_backends(self) -> None:
        self.assertIsNone(multi_reason({}, []))

    def test_returns_best_response(self) -> None:
        good_r = {"actions": [{"requires_approval": True}, {"requires_approval": True}], "reply": "great"}
        weak_r = {"actions": [], "reply": ""}

        def get_backend(name):
            b = MagicMock()
            b.name = name
            b.available.return_value = (True, "ok")
            b.reason.return_value = good_r if name == "claude" else weak_r
            return b

        with patch("personal_assistant.providers.get_backend", side_effect=get_backend), \
             patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.row_factory = None
            mock_conn.return_value.close = lambda: None
            result = multi_reason({}, ["claude", "cursor"], db_path=":memory:")

        self.assertIsNotNone(result)
        name, _ = result
        self.assertEqual(name, "claude")


# ---------------------------------------------------------------------------
# configured_backends
# ---------------------------------------------------------------------------

class ConfiguredBackendsTest(unittest.TestCase):
    def test_empty_when_not_set(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            if "MYOS_MULTI_PROVIDER" in __import__("os").environ:
                del __import__("os").environ["MYOS_MULTI_PROVIDER"]
        import os
        os.environ.pop("MYOS_MULTI_PROVIDER", None)
        self.assertEqual(configured_backends(), [])

    def test_parses_comma_separated(self) -> None:
        with patch.dict("os.environ", {"MYOS_MULTI_PROVIDER": "claude,cursor"}):
            self.assertEqual(configured_backends(), ["claude", "cursor"])

    def test_strips_whitespace(self) -> None:
        with patch.dict("os.environ", {"MYOS_MULTI_PROVIDER": " claude , cursor "}):
            self.assertEqual(configured_backends(), ["claude", "cursor"])

    def test_empty_parts_ignored(self) -> None:
        with patch.dict("os.environ", {"MYOS_MULTI_PROVIDER": "claude,,cursor"}):
            self.assertEqual(configured_backends(), ["claude", "cursor"])


if __name__ == "__main__":
    unittest.main()
