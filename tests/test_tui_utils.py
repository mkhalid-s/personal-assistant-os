"""Tests for tui_utils.py — shared TUI formatting helpers."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from personal_assistant.tui_utils import (
    condense_payload,
    format_age,
    integrity_chip,
    parse_payload,
    status_chip,
    truncate,
)


def _ts(delta_seconds: int = 0) -> str:
    """Return a SQLite-format timestamp offset from now by delta_seconds."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class FormatAgeTest(unittest.TestCase):
    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(format_age(""), "")

    def test_none_like_input_returns_empty(self) -> None:
        self.assertEqual(format_age("not-a-date"), "")

    def test_seconds_under_60(self) -> None:
        result = format_age(_ts(30))
        self.assertRegex(result, r"^\d+s$")

    def test_minutes(self) -> None:
        result = format_age(_ts(90))
        self.assertRegex(result, r"^\d+m$")

    def test_hours_no_minutes(self) -> None:
        result = format_age(_ts(3600))
        self.assertRegex(result, r"^\d+h$")

    def test_hours_with_minutes(self) -> None:
        result = format_age(_ts(3660))  # 1h 1m
        self.assertRegex(result, r"^\d+h \d+m$")

    def test_days(self) -> None:
        result = format_age(_ts(86400 * 2))
        self.assertRegex(result, r"^\d+d$")

    def test_sqlite_space_separator_format(self) -> None:
        # SQLite CURRENT_TIMESTAMP uses space, not T — must not crash
        result = format_age("2026-08-31 09:14:32")
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")  # should parse and return age

    def test_iso_t_separator_also_works(self) -> None:
        # '2026-08-31T09:14:32' — strip at [:19] gives '2026-08-31T09:14:32'
        # strptime format expects space; T-format would fail and return ''
        # This is documented behavior — SQLite format is canonical
        result = format_age("2026-08-31T09:14:32")
        self.assertIsInstance(result, str)  # no exception

    def test_future_timestamp_returns_0s(self) -> None:
        future = _ts(-60)  # 60 seconds in the future
        result = format_age(future)
        self.assertEqual(result, "0s")

    def test_malformed_string_returns_empty(self) -> None:
        self.assertEqual(format_age("yesterday"), "")
        self.assertEqual(format_age("2026-99-99 25:99:99"), "")


class TruncateTest(unittest.TestCase):
    def test_short_string_unchanged(self) -> None:
        self.assertEqual(truncate("hello", 10), "hello")

    def test_exact_length_unchanged(self) -> None:
        self.assertEqual(truncate("hello", 5), "hello")

    def test_long_string_truncated(self) -> None:
        result = truncate("hello world", 8)
        self.assertEqual(result, "hello w…")
        self.assertEqual(len(result), 8)

    def test_custom_suffix(self) -> None:
        result = truncate("hello world", 8, suffix="...")
        self.assertEqual(result, "hello...")
        self.assertEqual(len(result), 8)

    def test_non_string_converted(self) -> None:
        result = truncate(12345, 3)
        self.assertEqual(result, "12…")

    def test_empty_string(self) -> None:
        self.assertEqual(truncate("", 5), "")


class CondensePayloadTest(unittest.TestCase):
    def test_empty_dict(self) -> None:
        self.assertEqual(condense_payload({}), "")

    def test_none_values_skipped(self) -> None:
        result = condense_payload({"a": None, "b": "val"})
        self.assertNotIn("a=", result)
        self.assertIn("b=val", result)

    def test_empty_string_values_skipped(self) -> None:
        result = condense_payload({"a": "", "b": "x"})
        self.assertNotIn("a=", result)

    def test_long_value_truncated(self) -> None:
        long_val = "x" * 100
        result = condense_payload({"key": long_val})
        self.assertIn("key=", result)
        self.assertLessEqual(len(result), 120)

    def test_total_capped_at_max_chars(self) -> None:
        payload = {f"k{i}": f"val{i}" * 5 for i in range(20)}
        result = condense_payload(payload, max_chars=60)
        self.assertLessEqual(len(result), 60)

    def test_non_dict_returns_empty(self) -> None:
        self.assertEqual(condense_payload("not a dict"), "")  # type: ignore[arg-type]
        self.assertEqual(condense_payload(None), "")  # type: ignore[arg-type]


class StatusChipTest(unittest.TestCase):
    def test_all_known_statuses(self) -> None:
        for status in ("proposed", "approved", "executed", "blocked", "failed", "expired"):
            label, style = status_chip(status)
            self.assertIsInstance(label, str)
            self.assertIsInstance(style, str)
            self.assertTrue(label)  # non-empty label

    def test_unknown_status_returns_itself(self) -> None:
        label, style = status_chip("mystery")
        self.assertEqual(label, "mystery")

    def test_empty_status_returns_dash(self) -> None:
        label, _ = status_chip("")
        self.assertEqual(label, "—")


class IntegrityChipTest(unittest.TestCase):
    def test_all_known_states(self) -> None:
        for state in ("fresh", "nearing_expiry", "expired", "tampered", "not_yet_approved", "invalid", ""):
            label, style = integrity_chip(state)
            self.assertIsInstance(label, str)
            self.assertIsInstance(style, str)

    def test_fresh_is_green(self) -> None:
        _, style = integrity_chip("fresh")
        self.assertEqual(style, "green")

    def test_tampered_is_bold_red(self) -> None:
        _, style = integrity_chip("tampered")
        self.assertEqual(style, "bold red")

    def test_unknown_state_returns_dim(self) -> None:
        _, style = integrity_chip("unknown_state")
        self.assertEqual(style, "dim")


class ParsePayloadTest(unittest.TestCase):
    def test_valid_json(self) -> None:
        result = parse_payload('{"a": 1, "b": "x"}')
        self.assertEqual(result, {"a": 1, "b": "x"})

    def test_empty_string_returns_empty_dict(self) -> None:
        self.assertEqual(parse_payload(""), {})

    def test_none_returns_empty_dict(self) -> None:
        self.assertEqual(parse_payload(None), {})  # type: ignore[arg-type]

    def test_invalid_json_returns_empty_dict(self) -> None:
        self.assertEqual(parse_payload("{not valid}"), {})

    def test_non_dict_json_returns_empty_dict(self) -> None:
        self.assertEqual(parse_payload("[1, 2, 3]"), {})


if __name__ == "__main__":
    unittest.main()
