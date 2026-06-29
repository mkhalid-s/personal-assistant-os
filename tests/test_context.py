"""Tests for the Context Intelligence Loop (conversation logging, observation stream,
reflection, relationship-graph derivation, gated suggestions, scored retrieval, hygiene).

No network: a fake backend stands in for the brain; everything else is local SQLite.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _fresh_db_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["MYOS_DB_PATH"] = tmp.name
    from personal_assistant.db import get_connection

    return get_connection(), tmp.name


class ContextLoopTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    # -- Slice 1: logging -------------------------------------------------------------
    def test_log_turn_persists_and_is_recallable(self):
        from personal_assistant import context, queries

        out = context.log_turn(
            self.conn,
            user_text="Priya owns the auth token rotation rollout.",
            assistant_text="Noted — I'll track Priya's rollout.",
            surface="chat",
            backend="fake",
        )
        self.assertIn("conversation_id", out)
        self.assertIn("turn_id", out)
        # conversation + turn rows exist
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT turn_count FROM conversations").fetchone()["turn_count"], 1
        )
        # mirrored into searchable memory
        hits = queries.context_search(self.conn, "auth token rotation")
        self.assertTrue(any(h["source_type"] == "conversation" for h in hits), hits)

    def test_log_turn_redacts_pii(self):
        from personal_assistant import context

        context.log_turn(
            self.conn,
            user_text="ping me at john.doe@example.com about it",
            assistant_text="ok",
            backend="fake",
        )
        stored = self.conn.execute("SELECT user_text FROM conversation_turns").fetchone()["user_text"]
        self.assertNotIn("john.doe@example.com", stored)
        self.assertIn("[REDACTED_EMAIL]", stored)

    def test_logging_kill_switch(self):
        from personal_assistant import context

        self.conn.execute(
            "INSERT INTO assistant_policies (key, value) VALUES ('log_conversations', 'false')"
        )
        self.conn.commit()
        self.assertFalse(context.logging_enabled(self.conn))
        out = context.log_turn(self.conn, user_text="hi", assistant_text="hello", backend="fake")
        self.assertEqual(out, {})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0], 0)

    def test_turns_group_into_one_conversation(self):
        from personal_assistant import context

        first = context.log_turn(self.conn, user_text="a", assistant_text="1", backend="fake")
        cid = first["conversation_id"]
        context.log_turn(self.conn, user_text="b", assistant_text="2", conversation_id=cid, backend="fake")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT turn_count FROM conversations").fetchone()["turn_count"], 2)
        idxs = [r["turn_index"] for r in self.conn.execute("SELECT turn_index FROM conversation_turns ORDER BY id")]
        self.assertEqual(idxs, [0, 1])

    # -- Slice 2: observations, reflection, relationship edges -------------------------
    def test_extract_observations_people_commitment_preference_risk(self):
        from personal_assistant import context, em

        em.upsert_person(self.conn, "Priya", role="SE")
        em.upsert_person(self.conn, "Raj", role="EM")
        self.conn.commit()
        n = context.extract_observations(
            self.conn,
            None,
            "Priya and Raj are blocked on the migration; I'll follow up by Friday. "
            "Going forward, always summarize action items.",
            "ack",
        )
        self.conn.commit()
        kinds = {r["kind"] for r in self.conn.execute("SELECT kind FROM context_observations")}
        self.assertIn("person", kinds)
        self.assertIn("commitment", kinds)
        self.assertIn("preference", kinds)
        self.assertIn("risk", kinds)
        self.assertGreaterEqual(n, 4)

    def test_comention_edge_strengthens(self):
        from personal_assistant import context, em

        em.upsert_person(self.conn, "Priya")
        em.upsert_person(self.conn, "Raj")
        self.conn.commit()
        for _ in range(2):
            context.extract_observations(self.conn, None, "Priya synced with Raj on the rollout.", "ok")
        self.conn.commit()
        edge = self.conn.execute(
            "SELECT weight FROM knowledge_edges WHERE relation='co_mentioned' AND source='context'"
        ).fetchone()
        self.assertIsNotNone(edge)
        self.assertGreaterEqual(edge["weight"], 2.0)  # two co-mentions accumulated weight

    def test_reflect_creates_and_supersedes_insight(self):
        from personal_assistant import context, em

        em.upsert_person(self.conn, "Priya")
        self.conn.commit()
        context.extract_observations(self.conn, None, "Priya owns auth rotation.", "ok")
        context.extract_observations(self.conn, None, "Priya is blocked on auth rotation.", "ok")
        self.conn.commit()
        r1 = context.reflect(self.conn, min_cluster=2)
        self.assertGreaterEqual(r1["insights"], 1)
        # A second reflection supersedes the first (versioned, not duplicated open insights).
        context.extract_observations(self.conn, None, "Priya unblocked auth rotation.", "ok")
        self.conn.commit()
        context.reflect(self.conn, min_cluster=2)
        open_for_priya = self.conn.execute(
            "SELECT COUNT(*) FROM context_insights WHERE summary LIKE 'Recurring context around Priya (%' "
            "AND superseded_by IS NULL"
        ).fetchone()[0]
        self.assertEqual(open_for_priya, 1)

    def test_reflect_generates_gated_suggestions(self):
        from personal_assistant import context, em

        em.upsert_person(self.conn, "Priya")
        self.conn.commit()
        context.extract_observations(self.conn, None, "Priya is blocked on the migration; this is a risk.", "ok")
        context.extract_observations(self.conn, None, "Going forward, always summarize action items.", "ok")
        self.conn.commit()
        out = context.reflect(self.conn, min_cluster=1)
        self.assertGreaterEqual(out.get("suggestions", 0), 1)
        titles = [s["title"] for s in context.list_suggestions(self.conn)]
        self.assertTrue(any("risk for Priya" in t for t in titles), titles)
        self.assertTrue(any(t.startswith("Adopt standing preference") for t in titles), titles)
        # Re-running reflect does NOT duplicate the still-open suggestions (deduped).
        before = len(context.list_suggestions(self.conn))
        context.reflect(self.conn, min_cluster=1)
        self.assertEqual(len(context.list_suggestions(self.conn)), before)

    # -- Slice 3: suggestions + scored retrieval --------------------------------------
    def test_suggestion_lifecycle_and_feedback_logged(self):
        from personal_assistant import context

        sid = context.propose_suggestion(
            self.conn, title="Track Priya's rollout as a work item", rationale="mentioned 3x"
        )
        self.conn.commit()
        self.assertIsNotNone(sid)
        # dedup: an identical open suggestion is not re-created
        self.assertIsNone(context.propose_suggestion(self.conn, title="Track Priya's rollout as a work item"))
        res = context.decide_suggestion(self.conn, sid, "accepted", feedback="good idea")
        self.assertEqual(res["status"], "accepted")
        # the decision is logged back as a feedback observation (the loop learns)
        fb = self.conn.execute("SELECT COUNT(*) FROM context_observations WHERE kind='feedback'").fetchone()[0]
        self.assertEqual(fb, 1)
        self.assertEqual(context.list_suggestions(self.conn, status="proposed"), [])

    def test_scored_retrieve_ranks_and_reinforces(self):
        from personal_assistant import context

        context._insert_observation(self.conn, None, "preference", None, "always summarize the migration risks")
        context._insert_observation(self.conn, None, "topic", None, "unrelated lunch chatter")
        self.conn.commit()
        top = context.scored_retrieve(self.conn, "migration risks", limit=5)
        self.assertTrue(top)
        self.assertIn("migration", top[0]["detail"])
        # retrieval reinforces (access_count bumped) so it resists hygiene decay
        ac = self.conn.execute(
            "SELECT access_count FROM context_observations WHERE detail LIKE 'always summarize%'"
        ).fetchone()["access_count"]
        self.assertGreaterEqual(ac, 1)

    # -- Slice 4: hygiene -------------------------------------------------------------
    def test_hygiene_dedups_and_decays(self):
        from personal_assistant import context

        # Two exact duplicates (bypass write-time dedup by inserting directly).
        for _ in range(2):
            self.conn.execute(
                "INSERT INTO context_observations (turn_id, kind, subject, detail, importance) "
                "VALUES (NULL, 'topic', NULL, 'dup detail', 0.3)"
            )
        # A stale, low-importance, never-recalled observation -> should decay.
        self.conn.execute(
            "INSERT INTO context_observations (turn_id, kind, subject, detail, importance, created_at) "
            "VALUES (NULL, 'topic', NULL, 'old stale note', 0.2, datetime('now','-120 days'))"
        )
        self.conn.commit()
        res = context.hygiene(self.conn, decay_days=60, importance_floor=0.4)
        self.assertGreaterEqual(res["merged"], 1)  # one of the duplicates merged away
        self.assertGreaterEqual(res["decayed"], 1)  # the stale note decayed
        # nothing was deleted — hygiene only flips status
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_observations").fetchone()[0], 3)

    # -- Swarm-review remediation regressions -----------------------------------------
    def test_reflect_supersede_subject_with_like_wildcards(self):  # M3
        from personal_assistant import context, em

        # Two distinct subjects whose names contain LIKE metacharacters.
        em.upsert_person(self.conn, "A_B")
        em.upsert_person(self.conn, "AXB")
        self.conn.commit()
        for subj in ("A_B", "AXB"):
            context.extract_observations(self.conn, None, f"{subj} owns the rollout.", "ok")
            context.extract_observations(self.conn, None, f"{subj} is blocked.", "ok")
        self.conn.commit()
        context.reflect(self.conn, min_cluster=2)
        context.reflect(self.conn, min_cluster=2)  # second pass supersedes per-subject
        # Each subject must have exactly ONE open insight — the '_' in 'A_B' must not have
        # superseded 'AXB''s insight (LIKE-wildcard cross-contamination).
        for subj in ("A_B", "AXB"):
            n = self.conn.execute(
                "SELECT COUNT(*) FROM context_insights WHERE subject = ? AND superseded_by IS NULL", (subj,)
            ).fetchone()[0]
            self.assertEqual(n, 1, f"{subj} should have exactly one open insight, got {n}")

    def test_extract_observations_mines_assistant_text(self):  # M4
        from personal_assistant import context

        # Empty user text, but the assistant committed to something — must still be mined.
        n = context.extract_observations(self.conn, None, "", "I'll follow up on the release by Friday.")
        self.conn.commit()
        self.assertGreaterEqual(n, 1)
        row = self.conn.execute(
            "SELECT subject FROM context_observations WHERE kind='commitment'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["subject"], "MYOS")  # an assistant-side commitment is owned by MYOS

    def test_decide_suggestion_is_one_way(self):  # L1
        from personal_assistant import context

        sid = context.propose_suggestion(self.conn, title="Do the thing")
        self.conn.commit()
        self.assertEqual(context.decide_suggestion(self.conn, sid, "accepted")["status"], "accepted")
        # Re-deciding a decided suggestion is rejected; no second feedback observation.
        res = context.decide_suggestion(self.conn, sid, "dismissed")
        self.assertIn("error", res)
        fb = self.conn.execute("SELECT COUNT(*) FROM context_observations WHERE kind='feedback'").fetchone()[0]
        self.assertEqual(fb, 1)
        # accepted -> applied is the one allowed follow-through.
        self.assertEqual(context.decide_suggestion(self.conn, sid, "applied")["status"], "applied")

    def test_dismissed_suggestion_not_reproposed(self):  # L2
        from personal_assistant import context

        sid = context.propose_suggestion(self.conn, title="Track Priya risk")
        self.conn.commit()
        context.decide_suggestion(self.conn, sid, "dismissed")
        # Proposing the same title again is suppressed even though it's no longer 'proposed'.
        self.assertIsNone(context.propose_suggestion(self.conn, title="Track Priya risk"))

    def test_hygiene_optin_purge_deletes_old_stale(self):  # L4
        from personal_assistant import context

        self.conn.execute(
            "INSERT INTO context_observations (turn_id, kind, subject, detail, importance, status, created_at) "
            "VALUES (NULL, 'topic', NULL, 'ancient decayed note', 0.1, 'decayed', datetime('now','-200 days'))"
        )
        self.conn.commit()
        res = context.hygiene(self.conn, purge_days=90)
        self.assertGreaterEqual(res["purged"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_observations").fetchone()[0], 0)

    def test_log_turn_self_heals_stale_conversation_id(self):  # L7
        from personal_assistant import context

        out = context.log_turn(self.conn, user_text="hi", assistant_text="hello", conversation_id=99999, backend="fake")
        self.assertIn("conversation_id", out)
        self.assertNotEqual(out["conversation_id"], 99999)  # started a fresh one instead of crashing
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0], 1)

    def test_redaction_covers_secrets_ssn_card(self):  # M1
        from personal_assistant import context

        context.log_turn(
            self.conn,
            user_text="key sk-ABCDEFGHIJKLMNOPQRSTUV, ssn 123-45-6789, card 4111 1111 1111 1111",
            assistant_text="ok",
            backend="fake",
        )
        stored = self.conn.execute("SELECT user_text FROM conversation_turns").fetchone()["user_text"]
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUV", stored)
        self.assertNotIn("123-45-6789", stored)
        self.assertNotIn("4111 1111 1111 1111", stored)
        self.assertIn("[REDACTED_SECRET]", stored)

    def test_conversation_retention_purges_when_enabled(self):  # M2
        from personal_assistant import context
        from personal_assistant.privacy import _cleanup_policy_retention

        out = context.log_turn(self.conn, user_text="something old", assistant_text="ok", backend="fake")
        # Age the turn past the retention window and enable a finite policy.
        self.conn.execute("UPDATE conversation_turns SET created_at = datetime('now','-400 days')")
        self.conn.execute("INSERT INTO assistant_policies (key, value) VALUES ('retention_conversation_days','365')")
        self.conn.commit()
        res = _cleanup_policy_retention(self.conn)
        self.conn.commit()
        self.assertGreaterEqual(res["conversation_turns"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0], 0)
        # mirrored chunk purged from the index too
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM text_chunks WHERE source_type='conversation'").fetchone()[0], 0
        )

    # -- Integration: the run_turn chokepoint logs automatically ----------------------
    def test_run_turn_logs_through_chokepoint(self):
        from personal_assistant import assistant

        class FakeBackend:
            name = "fake"

            def run_turn(self, conn, user_text, history, on_text=None):
                return {
                    "reply": "tracked",
                    "proposed_action_ids": [],
                    "history": history + [{"role": "user", "content": user_text}],
                    "backend": "fake",
                }

        orig = assistant.get_backend
        assistant.get_backend = lambda name=None: FakeBackend()
        try:
            result = assistant.run_turn(self.conn, "Ship the release on Friday.", [], surface="chat")
        finally:
            assistant.get_backend = orig
        self.assertIn("conversation_id", result)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0], 1)
        row = self.conn.execute("SELECT user_text, assistant_text, backend FROM conversation_turns").fetchone()
        self.assertEqual(row["assistant_text"], "tracked")
        self.assertEqual(row["backend"], "fake")


class RoundTwoRemediationTest(unittest.TestCase):
    """Regressions for the round-2 swarm findings + the aside findings."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_media_retention_handles_fk_children(self):  # round-2 #1 (regression)
        from personal_assistant.privacy import _cleanup_policy_retention

        self.conn.execute(
            "INSERT INTO media_assets (id, media_type, file_path, created_at) "
            "VALUES (1, 'audio', '/x.wav', datetime('now','-90 days'))"
        )
        self.conn.execute("INSERT INTO media_imports (media_asset_id) VALUES (1)")
        self.conn.execute(
            "INSERT INTO file_ingests (watch_dir_id, file_path, file_hash, media_asset_id) "
            "VALUES (NULL, '/x.wav', 'h', 1)"
        )
        self.conn.commit()
        res = _cleanup_policy_retention(self.conn)  # must NOT raise a FK IntegrityError
        self.conn.commit()
        self.assertEqual(res["media"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM media_imports").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_ingests").fetchone()[0], 0)

    def test_retention_tolerates_non_integer_policy(self):  # round-2 #2
        from personal_assistant.privacy import _cleanup_policy_retention

        self.conn.execute(
            "INSERT OR REPLACE INTO assistant_policies (key, value) VALUES ('retention_media_days','never')"
        )
        self.conn.commit()
        res = _cleanup_policy_retention(self.conn)  # must fall back to default, not ValueError
        self.assertIn("media", res)

    def test_card_redaction_preserves_trailing_separator(self):  # round-2 #3/#11 (regression)
        from personal_assistant.privacy import apply_privacy_filters

        out = apply_privacy_filters(self.conn, "card 4111 1111 1111 1111 here")
        self.assertIn("[REDACTED_CARD]", out)
        self.assertIn("[REDACTED_CARD] here", out)  # the space before 'here' survives

    def test_stale_insight_is_expired(self):  # round-2 #4
        from personal_assistant import context, em

        em.upsert_person(self.conn, "Priya")
        self.conn.commit()
        context.extract_observations(self.conn, None, "Priya owns the rollout.", "ok")
        context.extract_observations(self.conn, None, "Priya is blocked.", "ok")
        self.conn.commit()
        context.reflect(self.conn, min_cluster=2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_insights WHERE subject='Priya' AND superseded_by IS NULL"
            ).fetchone()[0], 1
        )
        # Remove the supporting observations, then reflect again: the open insight must expire.
        self.conn.execute("UPDATE context_observations SET status='decayed' WHERE subject='Priya'")
        self.conn.commit()
        out = context.reflect(self.conn, min_cluster=2)
        self.assertGreaterEqual(out.get("expired", 0), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_insights WHERE subject='Priya' AND superseded_by IS NULL"
            ).fetchone()[0], 0
        )

    def test_remember_redacts_at_chokepoint(self):  # round-2 #6
        from personal_assistant import agentcore

        agentcore.remember(self.conn, "the deploy key is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
        self.conn.commit()
        stored = self.conn.execute(
            "SELECT content FROM text_chunks WHERE source_type='memory' ORDER BY id DESC LIMIT 1"
        ).fetchone()["content"]
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", stored)
        self.assertIn("[REDACTED_SECRET]", stored)

    def test_retention_purges_derived_insights_and_edges(self):  # round-2 #7/#8
        from personal_assistant import context, em
        from personal_assistant.privacy import _cleanup_policy_retention

        em.upsert_person(self.conn, "Priya")
        em.upsert_person(self.conn, "Raj")
        self.conn.commit()
        out = context.log_turn(self.conn, user_text="Priya synced with Raj.", assistant_text="ok", backend="fake")
        context.extract_observations(self.conn, out["turn_id"], "Priya synced with Raj again.", "ok")
        self.conn.commit()
        context.reflect(self.conn, min_cluster=1)
        # Age everything past the window and purge.
        for t in ("conversation_turns", "context_insights", "knowledge_edges"):
            self.conn.execute(f"UPDATE {t} SET created_at = datetime('now','-400 days')")
        self.conn.execute("INSERT INTO assistant_policies (key, value) VALUES ('retention_conversation_days','365')")
        self.conn.commit()
        _cleanup_policy_retention(self.conn)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_insights").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM knowledge_edges WHERE source='context'").fetchone()[0], 0
        )
        # the orphaned context-derived person nodes are gone too
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE node_type='person'").fetchone()[0], 0
        )

    def test_phone_redaction_not_over_eager(self):  # round-2 #9
        from personal_assistant.privacy import apply_privacy_filters

        # Real phone gets redacted...
        self.assertIn("[REDACTED_PHONE]", apply_privacy_filters(self.conn, "call me at 555-123-4567"))
        # ...but ordinary engineering numerics do NOT.
        for benign in ("released on 2024-06-25", "build took 12345 6789 ms", "ticket 1234567890"):
            self.assertNotIn("[REDACTED_PHONE]", apply_privacy_filters(self.conn, benign), benign)

    def test_card_redaction_separately_gated(self):  # round-2 #10
        from personal_assistant.privacy import apply_privacy_filters

        self.conn.execute("INSERT INTO assistant_policies (key, value) VALUES ('redact_cards','false')")
        self.conn.commit()
        out = apply_privacy_filters(self.conn, "card 4111 1111 1111 1111")
        self.assertIn("4111 1111 1111 1111", out)  # cards disabled, left intact
        # but secrets still redacted (separate gate)
        self.assertIn("[REDACTED_SECRET]", apply_privacy_filters(self.conn, "key ghp_ABCDEFGHIJKLMNOPQRSTUVWX12"))

    def test_agent_cli_audit_is_redacted(self):  # aside P1a
        from personal_assistant.providers.agent_cli import _audit

        _audit(
            self.conn, "copilot",
            {"purpose": "chat", "objective": "email me at jane@example.com"},
            "reply: call 555-123-4567", "ok", "", 5,
        )
        row = self.conn.execute("SELECT request_json, response_json FROM ai_provider_calls").fetchone()
        self.assertNotIn("jane@example.com", row["request_json"])
        self.assertNotIn("555-123-4567", row["response_json"])

    def test_requires_approval_coercion_is_crash_proof(self):  # aside P2a
        from personal_assistant.providers import _coerce_approval

        self.assertEqual(_coerce_approval("false"), 0)
        self.assertEqual(_coerce_approval("true"), 1)
        self.assertEqual(_coerce_approval(True), 1)
        self.assertEqual(_coerce_approval(0), 0)
        self.assertEqual(_coerce_approval("garbage"), 1)  # ambiguous -> fail safe (require approval)


class RoundThreeRemediationTest(unittest.TestCase):
    """Regressions for the round-3 swarm findings (wiring gaps + redaction chokepoints)."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_run_turn_coerces_string_requires_approval(self):  # round-3 #1/#5
        # A subprocess backend returning the very common JSON-bool-as-string "false" must NOT
        # crash run_turn (int("false") used to raise ValueError and kill the chat turn).
        from personal_assistant.providers import BaseBackend

        class StubBackend(BaseBackend):
            name = "stub"

            def reason(self, conn, request):
                return {"reply": "ok", "plan": [],
                        "actions": [{"action_type": "create_inbox_item", "title": "t",
                                     "payload": {}, "requires_approval": "false"}]}

        out = StubBackend().run_turn(self.conn, "do a thing", [])
        self.assertEqual(len(out["proposed_action_ids"]), 1)  # did not crash, enqueued
        ra = self.conn.execute(
            "SELECT requires_approval FROM agent_actions ORDER BY id DESC LIMIT 1"
        ).fetchone()["requires_approval"]
        self.assertEqual(ra, 0)  # "false" coerced to 0, not a ValueError

    def test_em_one_on_one_redacts_table_and_inbox(self):  # round-3 #2/#7
        from personal_assistant import em

        em.log_one_on_one(
            self.conn, "Priya",
            "1:1 with Priya, her cell is 555-123-4567. Action: email priya@example.com by Friday.",
            action_items=["call back at 555-987-6543"],
        )
        self.conn.commit()
        raw = self.conn.execute("SELECT raw_text FROM one_on_ones ORDER BY id DESC LIMIT 1").fetchone()["raw_text"]
        self.assertNotIn("555-123-4567", raw)
        self.assertNotIn("priya@example.com", raw)
        # model-supplied action_items must not reach inbox_items in cleartext (round-3 #2)
        items = " ".join(r["text"] for r in self.conn.execute("SELECT text FROM inbox_items").fetchall())
        self.assertNotIn("555-987-6543", items)
        self.assertIn("[REDACTED_PHONE]", items)

    def test_em_meeting_redacts_transcript_and_fts(self):  # round-3 #7
        from personal_assistant import em
        from personal_assistant.inbox import index_chunk

        res = em.capture_meeting(
            self.conn, "Standup",
            "We decided to ship. Bob will call the vendor at 555-222-3333 by Monday.",
        )
        # mirror triage indexing the meeting raw_text into the FTS-backed text_chunks
        raw = self.conn.execute("SELECT raw_text FROM meetings WHERE id=?", (res["meeting_id"],)).fetchone()["raw_text"]
        self.assertNotIn("555-222-3333", raw)
        index_chunk(self.conn, "work_item", 1, "Bob will call the vendor at 555-222-3333")
        self.conn.commit()
        chunk = self.conn.execute(
            "SELECT content FROM text_chunks WHERE source_type='work_item' ORDER BY id DESC LIMIT 1"
        ).fetchone()["content"]
        self.assertNotIn("555-222-3333", chunk)

    def test_index_chunk_redacts_work_item_titles(self):  # round-3 #3
        from personal_assistant.inbox import index_chunk

        index_chunk(self.conn, "work_item", 42, "follow up with secret token=supersecretvalue123")
        self.conn.commit()
        chunk = self.conn.execute(
            "SELECT content FROM text_chunks WHERE source_id=42"
        ).fetchone()["content"]
        self.assertNotIn("supersecretvalue123", chunk)
        self.assertIn("[REDACTED_SECRET]", chunk)

    def test_capture_item_redacts_inbox(self):  # round-3 #2/#8
        from personal_assistant import agentcore

        agentcore.capture_item(self.conn, text="ping me at 555-321-7654 about the deal", kind="note")
        self.conn.commit()
        text = self.conn.execute("SELECT text FROM inbox_items ORDER BY id DESC LIMIT 1").fetchone()["text"]
        self.assertNotIn("555-321-7654", text)
        self.assertIn("[REDACTED_PHONE]", text)

    def test_planner_legacy_audit_redacts_response(self):  # round-3 #6 (regression)
        # The legacy MYOS_AI_COMMAND finally-block writes the external agent's parsed stdout
        # into ai_provider_calls.response_json; that output can echo PII (here a phone in an
        # action title) and must be redacted like agent_cli._audit.
        from personal_assistant import planner

        # Emit a valid {plan, actions} JSON whose action title carries a phone number.
        os.environ["MYOS_AI_COMMAND"] = (
            "python3 -c \"import sys; "
            "sys.stdout.write('{\\\"plan\\\":[{\\\"step\\\":\\\"s\\\",\\\"detail\\\":\\\"call 555-444-3210\\\"}],"
            "\\\"actions\\\":[{\\\"action_type\\\":\\\"create_inbox_item\\\",\\\"title\\\":\\\"call 555-444-3210\\\","
            "\\\"payload\\\":{},\\\"requires_approval\\\":1}]}')\""
        )
        try:
            planner._ai_reason_artifacts(
                self.conn, purpose="chat", objective="hello", context="", analogies=[]
            )
        finally:
            os.environ.pop("MYOS_AI_COMMAND", None)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT response_json, error FROM ai_provider_calls ORDER BY id DESC LIMIT 1"
        ).fetchone()
        blob = (row["response_json"] or "") + (row["error"] or "")
        self.assertNotIn("555-444-3210", blob)
        self.assertIn("[REDACTED_PHONE]", row["response_json"] or "")

    def test_international_phone_is_redacted(self):  # round-3 #9
        from personal_assistant.privacy import apply_privacy_filters

        for intl in ("+44 20 7946 0958", "+91 98765 43210", "+33 1 23 45 67 89",
                     "+442079460958", "reach me tel:+15551234567 today"):
            out = apply_privacy_filters(self.conn, intl)
            self.assertIn("[REDACTED_PHONE]", out, intl)
        # still no over-redaction of the round-2 benign cases or disabled-card text
        for benign in ("released on 2024-06-25", "ticket 1234567890"):
            self.assertNotIn("[REDACTED_PHONE]", apply_privacy_filters(self.conn, benign), benign)

    def test_media_retention_purges_provenance_and_spares_fresh_conversation(self):  # round-3 #10
        from personal_assistant.privacy import _cleanup_policy_retention

        # Aged media + its 'file' provenance row.
        self.conn.execute(
            "INSERT INTO media_assets (media_type, file_path, transcript_text, source, created_at) "
            "VALUES ('file', '/tmp/old.wav', 'x', 'watch_dir', datetime('now','-90 days'))"
        )
        self.conn.execute(
            "INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet) "
            "VALUES ('file', '/tmp/old.wav', 'watch_dir', '1', 0.75, 'x')"
        )
        # A freshly started, still-empty conversation that must survive a conversation purge.
        self.conn.execute("INSERT INTO conversations (id, surface) VALUES (999, 'chat')")
        self.conn.execute("INSERT INTO assistant_policies (key, value) VALUES ('retention_conversation_days','30')")
        self.conn.commit()
        _cleanup_policy_retention(self.conn)
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM provenance WHERE source_ref='/tmp/old.wav'").fetchone()[0], 0
        )  # orphan provenance purged with its media
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversations WHERE id=999").fetchone()[0], 1
        )  # fresh empty conversation NOT purged (not aged past cutoff)


class RoundFourRemediationTest(unittest.TestCase):
    """Regression tests for round-4 findings (all pre-existing gaps + 4 true regressions)."""

    def setUp(self):
        self.conn, self.db_path = _fresh_db_conn()
        self.conn.execute("INSERT OR IGNORE INTO assistant_policies (key, value) VALUES ('retention_media_days','30')")
        self.conn.execute("INSERT OR IGNORE INTO assistant_policies (key, value) VALUES ('retention_evidence_days','365')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("MYOS_DB_PATH", None)

    def test_insert_inbox_item_dedup_redacts_pii(self):  # R4-1 + R4-3 inbox chokepoint
        from personal_assistant.inbox import insert_inbox_item_dedup

        insert_inbox_item_dedup(
            self.conn, text="call vendor at 555-111-2222 about contract",
            kind="task", owner=None, due_date=None, confidence=0.9, source="test"
        )
        self.conn.commit()
        text = self.conn.execute("SELECT text FROM inbox_items ORDER BY id DESC LIMIT 1").fetchone()["text"]
        self.assertNotIn("555-111-2222", text)
        self.assertIn("[REDACTED_PHONE]", text)

    def test_insert_inbox_item_dedup_collapse_on_pii_is_intentional(self):  # R4-5
        # Two inputs that differ only in PII (same text after redaction) produce one row.
        # This is the documented, expected behavior of deduping on the redacted form.
        from personal_assistant.inbox import insert_inbox_item_dedup

        id1 = insert_inbox_item_dedup(
            self.conn, text="call Bob at 555-111-2222", kind="task",
            owner=None, due_date=None, confidence=0.9, source="test"
        )
        id2 = insert_inbox_item_dedup(
            self.conn, text="call Bob at 555-333-4444", kind="task",
            owner=None, due_date=None, confidence=0.9, source="test"
        )
        self.conn.commit()
        self.assertIsNotNone(id1)
        self.assertIsNone(id2)  # second collapses — same redacted text + kind + source

    def test_enqueue_proposal_redacts_title_and_payload(self):  # R4-2
        from personal_assistant import agentcore

        task_id = agentcore.ensure_turn_task(self.conn, "test task")
        action_id = agentcore.enqueue_proposal(
            self.conn,
            task_id=task_id,
            action_type="draft_external_update",
            title="Follow up with jane@example.com about the deal",
            payload={"body": "ring me at 555-777-9999", "notes": "key: sk-abc123xyz456def789"},
            requires_approval=1,
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT title, payload_json FROM agent_actions WHERE id=?", (action_id,)
        ).fetchone()
        self.assertNotIn("jane@example.com", row["title"])
        self.assertNotIn("555-777-9999", row["payload_json"])
        self.assertNotIn("sk-abc123xyz456def789", row["payload_json"])

    def test_decide_suggestion_feedback_is_redacted(self):  # R4-4
        from personal_assistant.context import propose_suggestion, decide_suggestion

        # seed an observation so reflect can work, then propose manually
        self.conn.execute(
            "INSERT INTO context_observations (subject, kind, detail, importance, status) "
            "VALUES ('Alice', 'preference', 'prefers async updates', 0.8, 'active')"
        )
        self.conn.execute(
            "INSERT INTO context_suggestions (insight_id, title, rationale, suggested_action, status) "
            "VALUES (NULL, 'test suggestion', 'rationale', 'do it', 'proposed')"
        )
        self.conn.commit()
        sid = self.conn.execute(
            "SELECT id FROM context_suggestions ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        decide_suggestion(
            self.conn, sid, "dismissed",
            feedback="email me at secret@corp.com or call 555-888-1234 with updates"
        )
        row = self.conn.execute(
            "SELECT feedback FROM context_suggestions WHERE id=?", (sid,)
        ).fetchone()
        self.assertNotIn("secret@corp.com", row["feedback"])
        self.assertNotIn("555-888-1234", row["feedback"])

    def test_index_chunk_returns_bool(self):  # R4-6 — index_chunk return used by cmd_reindex
        from personal_assistant.inbox import index_chunk

        wrote = index_chunk(self.conn, "work_item", 99, "schedule meeting about Q3 roadmap")
        self.assertTrue(wrote)
        skipped = index_chunk(self.conn, "work_item", 100, "   ")  # whitespace-only
        self.assertFalse(skipped)
        # skipped item must NOT have written a row
        row = self.conn.execute(
            "SELECT id FROM text_chunks WHERE source_id=100"
        ).fetchone()
        self.assertIsNone(row)

    def test_e164_lookbehind_prevents_mid_token_over_redaction(self):  # R4-7
        from personal_assistant.privacy import apply_privacy_filters

        # Mid-token '+' preceded by a word char/dot/hyphen must NOT fire the E.164 branch.
        # The lookbehind (?<![\w.\-]) guards these cases; '-' is in the lookbehind class.
        no_redact_cases = [
            "JIRA-+12345678",  # '-' before '+' is in the lookbehind class
            "ver.+12345678",   # '.' before '+'
            "x+12345678",      # word char before '+'
            "+1234",           # too short (< 8 suffix digits after the first)
        ]
        for text in no_redact_cases:
            out = apply_privacy_filters(self.conn, text)
            self.assertNotIn("[REDACTED_PHONE]", out, msg=f"over-redacted: {text!r}")

        # Real international phones still get redacted.
        for intl in ("+44 20 7946 0958", "+15551234567", "+91 98765 43210"):
            out = apply_privacy_filters(self.conn, intl)
            self.assertIn("[REDACTED_PHONE]", out, msg=f"missed: {intl!r}")

    def test_e164_trailing_guard_no_partial_match_on_long_runs(self):  # R4-8
        from personal_assistant.privacy import apply_privacy_filters

        # A 21-digit run: the old pattern would partially redact (first 18 digits),
        # leaving a stray "000" remnant. With (?!\d), the whole run should NOT match.
        long_run = "+100000000000000000000"  # 21 digits — not a real phone
        out = apply_privacy_filters(self.conn, long_run)
        # Either fully redacted (if a valid-length phone is found) or fully kept.
        # Key invariant: no "PHONE]000" partial-redact artifact.
        self.assertNotIn("[REDACTED_PHONE]000", out)
        # Also confirm no partial stray digit tail
        self.assertNotIn("]000", out)

    def test_provenance_purge_spares_non_aged_media_sharing_file_path(self):  # R4-9 (regression)
        from personal_assistant.privacy import _cleanup_policy_retention

        # Two media assets share '/x/notes.txt': one aged (> 30 days), one fresh.
        self.conn.execute(
            "INSERT INTO media_assets (media_type, file_path, transcript_text, source, created_at) "
            "VALUES ('file', '/x/notes.txt', 'old', 'watch_dir', datetime('now','-60 days'))"
        )
        aged_id = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO media_assets (media_type, file_path, transcript_text, source, created_at) "
            "VALUES ('file', '/x/notes.txt', 'new', 'watch_dir', datetime('now','-1 days'))"
        )
        fresh_id = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        # Each ingest creates a provenance row keyed by file_path.
        self.conn.execute(
            "INSERT INTO provenance (source_type, source_ref, extractor, extractor_version, confidence, snippet) "
            "VALUES ('file', '/x/notes.txt', 'watch_dir', '1', 0.75, 'old notes')"
        )
        prov_id = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        self.conn.commit()
        _cleanup_policy_retention(self.conn)
        self.conn.commit()
        # Aged media deleted; fresh media survives.
        self.assertIsNone(
            self.conn.execute("SELECT id FROM media_assets WHERE id=?", (aged_id,)).fetchone()
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT id FROM media_assets WHERE id=?", (fresh_id,)).fetchone()
        )
        # Provenance must NOT be deleted — the path still has a surviving media asset.
        self.assertIsNotNone(
            self.conn.execute("SELECT id FROM provenance WHERE id=?", (prov_id,)).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
