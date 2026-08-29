from __future__ import annotations

import sqlite3
import unittest

from personal_assistant.claims import extract_claims, list_claims, record_claims
from personal_assistant.db import initialize_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class ExtractClaimsTest(unittest.TestCase):
    def test_sentence_with_cue_is_extracted(self) -> None:
        # _SENTENCE_RE = r"[^.!?\n]+" excludes the terminal punctuation, so
        # claim_text never includes the trailing period.
        claims = extract_claims("The API is broken.")
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["claim_text"], "The API is broken")
        self.assertAlmostEqual(claims[0]["confidence"], 0.72)

    def test_all_cue_words_trigger_extraction(self) -> None:
        cue_sentences = [
            "Auth is required.",
            "Users are authenticated.",
            "Service has a timeout.",
            "Nodes have limited memory.",
            "Build needs a fix.",
            "Deploy requires approval.",
            "Feature blocks the release.",
            "Auth depends on the SDK.",
            "Cache mitigates load spikes.",
            "Evidence supports the claim.",
            "Data confirms the hypothesis.",
        ]
        for sentence in cue_sentences:
            with self.subTest(sentence=sentence):
                claims = extract_claims(sentence)
                self.assertGreater(len(claims), 0, f"Expected claim from: {sentence!r}")

    def test_too_short_sentences_are_skipped(self) -> None:
        # Less than 12 chars total — should be filtered out.
        claims = extract_claims("X is bad.")
        self.assertEqual(claims, [])

    def test_too_long_sentences_are_skipped(self) -> None:
        long = "word " * 70 + "is repeated."  # well over 300 chars
        claims = extract_claims(long)
        self.assertEqual(claims, [])

    def test_no_cue_word_skips_sentence(self) -> None:
        claims = extract_claims("Everything went well today at the office.")
        self.assertEqual(claims, [])

    def test_duplicate_sentences_are_deduplicated(self) -> None:
        text = "The build is broken. The build is broken."
        claims = extract_claims(text)
        self.assertEqual(len(claims), 1)

    def test_case_insensitive_deduplication(self) -> None:
        text = "The build is broken. THE BUILD IS BROKEN."
        claims = extract_claims(text)
        self.assertEqual(len(claims), 1)

    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(extract_claims(""), [])

    def test_none_like_empty_handled(self) -> None:
        # The function guards with `text or ""`
        self.assertEqual(extract_claims(""), [])

    def test_multiple_distinct_claims_extracted(self) -> None:
        text = "Auth is required for all endpoints. Cache mitigates load spikes on high traffic."
        claims = extract_claims(text)
        self.assertEqual(len(claims), 2)

    def test_normalises_internal_whitespace(self) -> None:
        claims = extract_claims("The  service   is  down  today.")
        self.assertEqual(len(claims), 1)
        self.assertNotIn("  ", claims[0]["claim_text"])

    def test_multiline_text_split_by_newline(self) -> None:
        text = "Auth is required.\nCache mitigates failures."
        claims = extract_claims(text)
        self.assertEqual(len(claims), 2)


class RecordClaimsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_record_persists_claim(self) -> None:
        recorded = record_claims(self.conn, "The API is stable today.", source_type="note")
        self.conn.commit()
        self.assertEqual(len(recorded), 1)
        self.assertIn("id", recorded[0])
        self.assertGreater(recorded[0]["id"], 0)

    def test_record_deduplicates_same_source(self) -> None:
        record_claims(self.conn, "Auth is required.", source_type="note", source_id=1)
        self.conn.commit()
        record_claims(self.conn, "Auth is required.", source_type="note", source_id=1)
        self.conn.commit()
        rows = list_claims(self.conn, source_type="note")
        texts = [r["claim_text"] for r in rows]
        # Terminal period stripped by _SENTENCE_RE
        self.assertEqual(texts.count("Auth is required"), 1)

    def test_record_same_text_different_source_id_is_separate(self) -> None:
        record_claims(self.conn, "Auth is required.", source_type="note", source_id=1)
        self.conn.commit()
        record_claims(self.conn, "Auth is required.", source_type="note", source_id=2)
        self.conn.commit()
        rows = list_claims(self.conn, source_type="note")
        self.assertEqual(len(rows), 2)

    def test_record_returns_source_type_and_source_id(self) -> None:
        recorded = record_claims(self.conn, "The system is stable.", source_type="work_item", source_id=42)
        self.conn.commit()
        self.assertEqual(recorded[0]["source_type"], "work_item")
        self.assertEqual(recorded[0]["source_id"], "42")

    def test_record_empty_text_returns_empty(self) -> None:
        result = record_claims(self.conn, "")
        self.assertEqual(result, [])

    def test_record_no_cue_words_returns_empty(self) -> None:
        result = record_claims(self.conn, "Everything went fine today at the office meeting.")
        self.assertEqual(result, [])


class ListClaimsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_list_returns_all_when_no_filter(self) -> None:
        record_claims(self.conn, "Auth is required.", source_type="note")
        record_claims(self.conn, "Cache mitigates load.", source_type="work_item")
        self.conn.commit()
        rows = list_claims(self.conn)
        self.assertEqual(len(rows), 2)

    def test_list_filters_by_source_type(self) -> None:
        record_claims(self.conn, "Auth is required.", source_type="note")
        record_claims(self.conn, "Cache mitigates load.", source_type="work_item")
        self.conn.commit()
        rows = list_claims(self.conn, source_type="note")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_type"], "note")

    def test_list_respects_limit(self) -> None:
        for i in range(5):
            record_claims(self.conn, f"Service {i} is required for auth.", source_type="note", source_id=i)
        self.conn.commit()
        rows = list_claims(self.conn, limit=2)
        self.assertLessEqual(len(rows), 2)

    def test_list_empty_db_returns_empty(self) -> None:
        self.assertEqual(list_claims(self.conn), [])

    def test_list_rows_have_expected_keys(self) -> None:
        record_claims(self.conn, "Auth is required.", source_type="note")
        self.conn.commit()
        rows = list_claims(self.conn)
        self.assertIn("id", rows[0])
        self.assertIn("claim_text", rows[0])
        self.assertIn("confidence", rows[0])
        self.assertIn("source_type", rows[0])


if __name__ == "__main__":
    unittest.main()
