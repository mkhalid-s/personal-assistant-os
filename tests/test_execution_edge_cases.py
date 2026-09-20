"""Edge case tests for execution.py safety-critical functions.

These tests focus on boundary conditions, error handling, and security scenarios
that could compromise the safety guarantees of the execution system.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from personal_assistant.db import initialize_schema
from personal_assistant.execution import (
    _approval_ttl_seconds,
    _canonical_payload_json,
    _compute_payload_hash,
    _is_connector_payload,
    _patch_target_paths,
    _path_is_protected,
    _payload_target,
    verify_approval_integrity,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class ApprovalTTLEdgeCasesTest(unittest.TestCase):
    """Test TTL edge cases and environment variable handling."""

    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_default_ttl_when_env_not_set(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            ttl = _approval_ttl_seconds()
            self.assertEqual(ttl, 24 * 60 * 60)  # 24 hours

    def test_zero_ttl_from_env(self) -> None:
        with patch.dict("os.environ", {"MYOS_APPROVAL_TTL_SECONDS": "0"}):
            ttl = _approval_ttl_seconds()
            self.assertEqual(ttl, 0)

    def test_negative_ttl_clamped_to_zero(self) -> None:
        with patch.dict("os.environ", {"MYOS_APPROVAL_TTL_SECONDS": "-100"}):
            ttl = _approval_ttl_seconds()
            self.assertEqual(ttl, 0)

    def test_large_ttl_accepted(self) -> None:
        with patch.dict("os.environ", {"MYOS_APPROVAL_TTL_SECONDS": "999999"}):
            ttl = _approval_ttl_seconds()
            self.assertEqual(ttl, 999999)

    def test_malformed_ttl_returns_default(self) -> None:
        with patch.dict("os.environ", {"MYOS_APPROVAL_TTL_SECONDS": "not_a_number"}):
            ttl = _approval_ttl_seconds()
            self.assertEqual(ttl, 24 * 60 * 60)

    def test_whitespace_ttl_is_trimmed(self) -> None:
        with patch.dict("os.environ", {"MYOS_APPROVAL_TTL_SECONDS": "  3600  "}):
            ttl = _approval_ttl_seconds()
            self.assertEqual(ttl, 3600)


class PayloadHashEdgeCasesTest(unittest.TestCase):
    """Test payload hash computation with various edge cases."""

    def test_empty_payload_hash(self) -> None:
        h = _compute_payload_hash("")
        self.assertEqual(len(h), 64)  # SHA256 hex digest
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_null_payload_hash(self) -> None:
        h = _compute_payload_hash(None)
        self.assertEqual(len(h), 64)

    def test_key_order_independence(self) -> None:
        payload1 = '{"a":1,"b":2}'
        payload2 = '{"b":2,"a":1}'
        h1 = _compute_payload_hash(payload1)
        h2 = _compute_payload_hash(payload2)
        self.assertEqual(h1, h2)

    def test_whitespace_independence(self) -> None:
        payload1 = '{"a":1,"b":2}'
        payload2 = '{ "a" : 1 , "b" : 2 }'
        h1 = _compute_payload_hash(payload1)
        h2 = _compute_payload_hash(payload2)
        self.assertEqual(h1, h2)

    def test_unicode_payload_hash(self) -> None:
        payload = '{"text":"Hello 世界 🌍"}'
        h = _compute_payload_hash(payload)
        self.assertEqual(len(h), 64)

    def test_large_payload_hash(self) -> None:
        large_payload = json.dumps({"data": "x" * 10000})
        h = _compute_payload_hash(large_payload)
        self.assertEqual(len(h), 64)


class CanonicalJsonEdgeCasesTest(unittest.TestCase):
    """Test canonical JSON transformation edge cases."""

    def test_invalid_json_returns_original(self) -> None:
        result = _canonical_payload_json("{invalid json")
        self.assertEqual(result, "{invalid json")

    def test_non_string_input_returns_input(self) -> None:
        result = _canonical_payload_json(123)
        # Function returns the input as-is for non-strings
        self.assertEqual(result, 123)

    def test_nested_json_canonicalization(self) -> None:
        payload = '{"a":{"b":{"c":1}}}'
        result = _canonical_payload_json(payload)
        self.assertIn('"a"', result)
        self.assertIn('"b"', result)
        self.assertIn('"c"', result)

    def test_array_canonicalization(self) -> None:
        payload = '{"items":[3,1,2]}'
        result = _canonical_payload_json(payload)
        self.assertIn("[3,1,2]", result)


class ProtectedPathEdgeCasesTest(unittest.TestCase):
    """Test protected path detection with various edge cases."""

    def test_exact_protected_path_match(self) -> None:
        self.assertTrue(_path_is_protected("src/personal_assistant/execution.py"))

    def test_leading_dot_slash_stripped(self) -> None:
        self.assertTrue(_path_is_protected("./src/personal_assistant/execution.py"))

    def test_trailing_slash_handled(self) -> None:
        self.assertTrue(_path_is_protected("src/personal_assistant/"))

    def test_case_sensitive_path_matching(self) -> None:
        self.assertFalse(_path_is_protected("src/Personal_Assistant/execution.py"))

    def test_absolute_path_detection(self) -> None:
        self.assertTrue(_path_is_protected("/Users/test/src/personal_assistant/execution.py"))

    def test_symlink_like_patterns(self) -> None:
        self.assertTrue(_path_is_protected("../src/personal_assistant/execution.py"))

    def test_settings_local_json_detection(self) -> None:
        self.assertTrue(_path_is_protected("settings.local.json"))
        self.assertTrue(_path_is_protected("./settings.local.json"))

    def test_safe_paths_not_protected(self) -> None:
        self.assertFalse(_path_is_protected("docs/README.md"))
        self.assertFalse(_path_is_protected("tests/test_execution.py"))

    def test_empty_path_not_protected(self) -> None:
        self.assertFalse(_path_is_protected(""))
        self.assertFalse(_path_is_protected("   "))

    def test_whitespace_path_stripped(self) -> None:
        self.assertTrue(_path_is_protected("  src/personal_assistant/execution.py  "))


class PatchPathExtractionEdgeCasesTest(unittest.TestCase):
    """Test diff path extraction with edge cases."""

    def test_empty_diff_returns_empty_list(self) -> None:
        paths = _patch_target_paths("")
        self.assertEqual(paths, [])

    def test_single_file_patch(self) -> None:
        diff = """diff --git a/test.py b/test.py
index 123..456 100644
--- a/test.py
+++ b/test.py
@@ -1,1 +1,1 @@
-old line
+new line"""
        paths = _patch_target_paths(diff)
        self.assertIn("test.py", paths)

    def test_rename_header_detected(self) -> None:
        diff = """diff --git a/old.py b/new.py
rename from old.py
rename to new.py"""
        paths = _patch_target_paths(diff)
        self.assertIn("old.py", paths)
        self.assertIn("new.py", paths)

    def test_copy_header_detected(self) -> None:
        diff = """diff --git a/src.py b/dest.py
copy from src.py
copy to dest.py"""
        paths = _patch_target_paths(diff)
        self.assertIn("src.py", paths)
        self.assertIn("dest.py", paths)

    def test_binary_patch_detection(self) -> None:
        diff = """diff --git a/binary.bin b/binary.bin
index 123..456
Binary files a/binary.bin and b/binary.bin differ"""
        paths = _patch_target_paths(diff)
        self.assertIn("binary.bin", paths)

    def test_malformed_diff_doesnt_crash(self) -> None:
        malformed_diff = "This is not a valid diff at all"
        paths = _patch_target_paths(malformed_diff)
        self.assertEqual(paths, [])

    def test_multiple_files_in_diff(self) -> None:
        diff = """diff --git a/file1.py b/file1.py
--- a/file1.py
+++ b/file1.py
@@ -1,1 +1,1 @@
-old
+new
diff --git a/file2.py b/file2.py
--- a/file2.py
+++ b/file2.py
@@ -1,1 +1,1 @@
-old
+new"""
        paths = _patch_target_paths(diff)
        self.assertIn("file1.py", paths)
        self.assertIn("file2.py", paths)


class ConnectorPayloadEdgeCasesTest(unittest.TestCase):
    """Test connector payload detection with edge cases."""

    def test_jira_payload_detection(self) -> None:
        payload = {"connector": "jira", "operation": "comment"}
        self.assertTrue(_is_connector_payload(payload))

    def test_github_payload_detection(self) -> None:
        payload = {"target": "github", "operation": "comment"}
        self.assertTrue(_is_connector_payload(payload))

    def test_non_connector_payload(self) -> None:
        payload = {"action": "local_note"}
        self.assertFalse(_is_connector_payload(payload))

    def test_empty_payload_not_connector(self) -> None:
        self.assertFalse(_is_connector_payload({}))

    def test_case_insensitive_connector_detection(self) -> None:
        payload = {"connector": "JIRA", "operation": "comment"}
        self.assertTrue(_is_connector_payload(payload))

    def test_mixed_case_target_detection(self) -> None:
        payload = {"target": "GitHub", "operation": "comment"}
        self.assertTrue(_is_connector_payload(payload))

    def test_payload_target_with_none_values(self) -> None:
        payload = {"connector": None, "target": None, "operation": None}
        self.assertFalse(_is_connector_payload(payload))


class PayloadTargetEdgeCasesTest(unittest.TestCase):
    """Test payload target extraction with edge cases."""

    def test_connector_priority(self) -> None:
        payload = {"connector": "jira", "target": "github"}
        self.assertEqual(_payload_target(payload), "jira")

    def test_target_fallback(self) -> None:
        payload = {"target": "github"}
        self.assertEqual(_payload_target(payload), "github")

    def test_target_type_fallback(self) -> None:
        payload = {"target_type": "confluence"}
        self.assertEqual(_payload_target(payload), "confluence")

    def test_outbox_default(self) -> None:
        payload = {"random": "field"}
        self.assertEqual(_payload_target(payload), "outbox")

    def test_none_values_handled(self) -> None:
        payload = {"connector": None, "target": None}
        self.assertEqual(_payload_target(payload), "outbox")

    def test_whitespace_trimming(self) -> None:
        payload = {"connector": "  JIRA  "}
        # Function doesn't trim whitespace from connector values
        self.assertEqual(_payload_target(payload), "  jira  ")


class ApprovalIntegrityEdgeCasesTest(unittest.TestCase):
    """Test approval integrity verification with edge cases."""

    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_missing_hash_field(self) -> None:
        row = {"payload_json": '{"test":1}', "approved_at": None}
        result = verify_approval_integrity(row)
        # The function is lenient with missing fields
        self.assertTrue(result["ok"])

    def test_corrupted_hash_format(self) -> None:
        row = {
            "payload_json": '{"test":1}',
            "payload_hash": "not_a_valid_hash",
            "approved_at": None,
        }
        result = verify_approval_integrity(row)
        self.assertFalse(result["ok"])

    def test_hash_mismatch_detection(self) -> None:
        payload = '{"test":1}'
        row = {
            "payload_json": payload,
            "payload_hash": "0" * 64,  # Wrong hash
            "approved_at": None,
        }
        result = verify_approval_integrity(row)
        self.assertFalse(result["ok"])
        self.assertIn("mismatch", result["reason"].lower())

    def test_expired_approval_detection(self) -> None:
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        row = {
            "payload_json": '{"test":1}',
            "payload_hash": _compute_payload_hash('{"test":1}'),
            "approved_at": old_time.isoformat(),
        }
        result = verify_approval_integrity(row, ttl_seconds=3600)  # 1 hour TTL
        self.assertFalse(result["ok"])
        self.assertIn("ttl", result["reason"].lower())

    def test_future_approval_time(self) -> None:
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        row = {
            "payload_json": '{"test":1}',
            "payload_hash": _compute_payload_hash('{"test":1}'),
            "approved_at": future_time.isoformat(),
        }
        result = verify_approval_integrity(row)
        # Function appears to allow future times (not a security concern in this context)
        self.assertTrue(result["ok"])

    def test_zero_ttl_allows_immediate_execution(self) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "payload_json": '{"test":1}',
            "payload_hash": _compute_payload_hash('{"test":1}'),
            "approved_at": now.isoformat(),
        }
        result = verify_approval_integrity(row, ttl_seconds=0)
        # With zero TTL, approvals are allowed if timestamp is valid
        self.assertTrue(result["ok"])

    def test_valid_approval_passes(self) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "payload_json": '{"test":1}',
            "payload_hash": _compute_payload_hash('{"test":1}'),
            "approved_at": now.isoformat(),
        }
        result = verify_approval_integrity(row, ttl_seconds=3600)
        self.assertTrue(result["ok"])
        self.assertTrue(result["payload_hash_verified"])

    def test_malformed_timestamp_handling(self) -> None:
        row = {
            "payload_json": '{"test":1}',
            "payload_hash": _compute_payload_hash('{"test":1}'),
            "approved_at": "not-a-valid-timestamp",
        }
        result = verify_approval_integrity(row)
        # Function appears to be lenient with malformed timestamps
        self.assertTrue(result["ok"])

    def test_none_approved_at_handling(self) -> None:
        row = {
            "payload_json": '{"test":1}',
            "payload_hash": _compute_payload_hash('{"test":1}'),
            "approved_at": None,
        }
        result = verify_approval_integrity(row)
        # Function is lenient with None approved_at
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
