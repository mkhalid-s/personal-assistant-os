"""Tests for context_budget.py — token estimation and history trimming."""

from __future__ import annotations

import unittest

from personal_assistant.context_budget import (
    check_budget,
    context_limit_for,
    estimate_tokens,
    messages_token_estimate,
    trim_history,
)


class EstimateTokensTest(unittest.TestCase):
    def test_empty_string(self) -> None:
        self.assertEqual(estimate_tokens(""), 1)  # max(1, 0//4)

    def test_four_chars_is_one_token(self) -> None:
        self.assertEqual(estimate_tokens("abcd"), 1)

    def test_scales_linearly(self) -> None:
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_long_text(self) -> None:
        text = "word " * 1000  # ~5000 chars → ~1250 tokens
        self.assertGreater(estimate_tokens(text), 1000)


class MessagesTokenEstimateTest(unittest.TestCase):
    def test_empty_list(self) -> None:
        self.assertEqual(messages_token_estimate([]), 0)

    def test_string_content(self) -> None:
        msgs = [{"role": "user", "content": "a" * 400}]
        self.assertEqual(messages_token_estimate(msgs), 100)

    def test_list_content_blocks(self) -> None:
        msgs = [{"role": "assistant", "content": [{"type": "text", "text": "a" * 400}]}]
        self.assertEqual(messages_token_estimate(msgs), 100)

    def test_multiple_messages_sum(self) -> None:
        msgs = [
            {"role": "user", "content": "a" * 400},
            {"role": "assistant", "content": "b" * 400},
        ]
        self.assertEqual(messages_token_estimate(msgs), 200)


class ContextLimitForTest(unittest.TestCase):
    def test_claude_sonnet(self) -> None:
        self.assertEqual(context_limit_for("claude-sonnet-4-5"), 200_000)

    def test_claude_opus(self) -> None:
        self.assertEqual(context_limit_for("claude-opus-4"), 200_000)

    def test_unknown_model_uses_default(self) -> None:
        self.assertEqual(context_limit_for("gpt-4"), 200_000)

    def test_empty_string(self) -> None:
        self.assertEqual(context_limit_for(""), 200_000)


class CheckBudgetTest(unittest.TestCase):
    def test_small_messages_ok(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        ok, estimated, limit = check_budget(msgs, model="claude-sonnet")
        self.assertTrue(ok)
        self.assertGreater(limit, estimated)

    def test_huge_messages_not_ok(self) -> None:
        # 200k tokens × 4 chars = 800k chars → exceeds 200k token limit
        msgs = [{"role": "user", "content": "a" * 900_000}]
        ok, estimated, limit = check_budget(msgs, model="claude-sonnet")
        self.assertFalse(ok)
        self.assertGreater(estimated, limit)


class TrimHistoryTest(unittest.TestCase):
    def _make_messages(self, n_turns: int, chars_per_turn: int = 100) -> list[dict]:
        msgs = []
        for i in range(n_turns):
            msgs.append({"role": "user", "content": f"question {i}: " + "a" * chars_per_turn})
            msgs.append({"role": "assistant", "content": f"answer {i}: " + "b" * chars_per_turn})
        return msgs

    def test_keeps_last_n_when_over(self) -> None:
        msgs = self._make_messages(30)  # 60 messages
        trimmed = trim_history(msgs, keep_last=10)
        self.assertLessEqual(len(trimmed), 10)

    def test_unchanged_when_under_limit(self) -> None:
        msgs = self._make_messages(5)  # 10 messages
        trimmed = trim_history(msgs, keep_last=40)
        self.assertEqual(len(trimmed), 10)

    def test_preserves_system_message(self) -> None:
        sys_msg = {"role": "system", "content": "You are a helpful assistant."}
        conv = self._make_messages(20)  # 40 messages
        msgs = [sys_msg] + conv
        trimmed = trim_history(msgs, keep_last=10)
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertEqual(trimmed[0]["content"], "You are a helpful assistant.")

    def test_empty_history(self) -> None:
        self.assertEqual(trim_history([]), [])

    def test_budget_token_enforcement(self) -> None:
        # Each turn has ~50 chars ≈ 12 tokens; 30 turns = 60 messages ≈ 720 tokens.
        msgs = self._make_messages(30, chars_per_turn=50)
        # Budget = 100 tokens → should trim to fit.
        trimmed = trim_history(msgs, keep_last=60, budget_tokens=100)
        estimated = messages_token_estimate(trimmed)
        self.assertLessEqual(estimated, 100)

    def test_keeps_newest_turns(self) -> None:
        msgs = [
            {"role": "user", "content": "oldest question"},
            {"role": "assistant", "content": "oldest answer"},
            {"role": "user", "content": "newest question"},
            {"role": "assistant", "content": "newest answer"},
        ]
        trimmed = trim_history(msgs, keep_last=2)
        self.assertEqual(len(trimmed), 2)
        self.assertIn("newest", trimmed[-1]["content"])

    def test_returns_copy_not_mutating_original(self) -> None:
        msgs = self._make_messages(5)
        original_len = len(msgs)
        trim_history(msgs, keep_last=2)
        self.assertEqual(len(msgs), original_len)


if __name__ == "__main__":
    unittest.main()
