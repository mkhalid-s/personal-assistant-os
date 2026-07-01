from __future__ import annotations

import unittest


class AutonomyPolicyDecisionTest(unittest.TestCase):
    def test_command_decisions_cover_allowed_approval_and_blocked(self) -> None:
        from personal_assistant import autonomy

        read_only = autonomy.decide_command("context", safety="read_only", level="balanced")
        self.assertEqual(read_only["decision"], "allowed")
        self.assertEqual(read_only["tier"], autonomy.AUTO)

        local_write = autonomy.decide_command("capture", safety="local_write", level="balanced")
        self.assertEqual(local_write["decision"], "allowed")
        self.assertEqual(local_write["tier"], autonomy.AUTO)

        external = autonomy.decide_command("sync", safety="external_write", level="balanced")
        self.assertEqual(external["decision"], "needs_approval")
        self.assertEqual(external["tier"], autonomy.CONFIRM)

        blocked = autonomy.decide_command("delete-everything", safety="unknown", level="bold")
        self.assertEqual(blocked["decision"], autonomy.BLOCKED)
        self.assertTrue(blocked["requires_approval"])


if __name__ == "__main__":
    unittest.main()
