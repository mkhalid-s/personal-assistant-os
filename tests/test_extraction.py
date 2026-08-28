from __future__ import annotations

import unittest

from personal_assistant.extraction import SuggestedItem, extract_suggestions


class ExtractSuggestionsKindsTest(unittest.TestCase):
    """Each sentence-classification branch is exercised independently."""

    def test_decision_starts_with_keyword(self) -> None:
        items = extract_suggestions("Decision: we will ship v2 next week.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "decision")
        self.assertAlmostEqual(items[0].confidence, 0.85)

    def test_decision_contains_we_decided(self) -> None:
        items = extract_suggestions("After the meeting we decided to delay the release.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "decision")

    def test_commitment_follow_up(self) -> None:
        items = extract_suggestions("I will follow up on the ticket by Thursday.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "commitment")
        self.assertAlmostEqual(items[0].confidence, 0.75)

    def test_commitment_i_will(self) -> None:
        items = extract_suggestions("I will review the PR before noon.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "commitment")

    def test_commitment_contraction(self) -> None:
        items = extract_suggestions("I'll send the updated spec tomorrow.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "commitment")

    def test_commitment_by_keyword(self) -> None:
        items = extract_suggestions("Deliver the draft by Friday.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "commitment")

    def test_risk_blocked(self) -> None:
        items = extract_suggestions("The build is blocked on the infra team.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "risk")
        self.assertAlmostEqual(items[0].confidence, 0.8)

    def test_risk_blocker(self) -> None:
        items = extract_suggestions("There is a blocker with the auth service.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "risk")

    def test_risk_risk_keyword(self) -> None:
        items = extract_suggestions("High risk of missing the deadline this sprint.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "risk")

    def test_risk_dependency(self) -> None:
        items = extract_suggestions("We have a dependency on the data pipeline team.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "risk")

    def test_task_todo(self) -> None:
        items = extract_suggestions("TODO: add logging to the retry loop.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "task")
        self.assertAlmostEqual(items[0].confidence, 0.7)

    def test_task_task_keyword(self) -> None:
        items = extract_suggestions("This task needs to be split into subtasks.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "task")

    def test_task_implement(self) -> None:
        items = extract_suggestions("We need to implement the new pagination API.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "task")

    def test_task_next_step(self) -> None:
        items = extract_suggestions("Next step is to refactor the connector layer.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "task")

    def test_note_fallback_no_match(self) -> None:
        items = extract_suggestions("This is just a general observation about the project.")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "note")
        self.assertAlmostEqual(items[0].confidence, 0.5)

    def test_note_preserves_original_text(self) -> None:
        text = "Some generic remark."
        items = extract_suggestions(text)
        self.assertEqual(items[0].text, text.strip())


class ExtractSuggestionsEdgeCasesTest(unittest.TestCase):
    """Boundary inputs and edge cases."""

    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(extract_suggestions(""), [])

    def test_whitespace_only_returns_empty(self) -> None:
        self.assertEqual(extract_suggestions("   \n\t  "), [])

    def test_case_insensitive_decision(self) -> None:
        items = extract_suggestions("DECISION: adopt the new framework.")
        self.assertEqual(items[0].kind, "decision")

    def test_case_insensitive_we_decided(self) -> None:
        items = extract_suggestions("After debate We Decided to move forward.")
        self.assertEqual(items[0].kind, "decision")

    def test_case_insensitive_commitment(self) -> None:
        items = extract_suggestions("I WILL prepare the slides.")
        self.assertEqual(items[0].kind, "commitment")

    def test_case_insensitive_risk(self) -> None:
        items = extract_suggestions("The RISK of failure is high.")
        self.assertEqual(items[0].kind, "risk")

    def test_case_insensitive_task(self) -> None:
        items = extract_suggestions("IMPLEMENT the new cache layer.")
        self.assertEqual(items[0].kind, "task")

    def test_leading_trailing_whitespace_stripped(self) -> None:
        items = extract_suggestions("   TODO: clean up the logs.   ")
        self.assertEqual(items[0].kind, "task")

    def test_sentence_text_is_trimmed(self) -> None:
        # The sentence splitter re.split(r"[.!?]\s+", ...) consumes the
        # terminating punctuation character, so stored text has no trailing dot.
        items = extract_suggestions("  Decision: go live on Monday.  ")
        self.assertEqual(items[0].text, "Decision: go live on Monday")

    def test_single_word_no_match_returns_note(self) -> None:
        items = extract_suggestions("Understood")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "note")


class ExtractSuggestionsMultiSentenceTest(unittest.TestCase):
    """Multi-sentence inputs produce one item per matching sentence."""

    def test_two_sentences_different_kinds(self) -> None:
        text = "Decision: we ship Friday. The build is blocked on infra."
        items = extract_suggestions(text)
        kinds = [i.kind for i in items]
        self.assertIn("decision", kinds)
        self.assertIn("risk", kinds)
        self.assertEqual(len(items), 2)

    def test_three_sentences_mixed(self) -> None:
        # "We decided" at the start of a sentence now matches via \bwe decided\b
        # (the old " we decided " space-delimited check missed sentence-initial
        # occurrences — fixed in extraction.py).
        text = (
            "We decided to freeze the API. "
            "I will write the migration script. "
            "TODO: update the docs."
        )
        items = extract_suggestions(text)
        self.assertEqual(len(items), 3)
        kinds = [i.kind for i in items]
        self.assertEqual(kinds, ["decision", "commitment", "task"])

    def test_no_note_fallback_when_some_sentences_match(self) -> None:
        text = "Decision: adopt postgres. This is just context."
        items = extract_suggestions(text)
        # Only the matching sentence produces an item; the non-matching one
        # does NOT trigger the note fallback (fallback is whole-text only).
        kinds = [i.kind for i in items]
        self.assertNotIn("note", kinds)

    def test_exclamation_and_question_split(self) -> None:
        text = "Is there a risk here? Definitely blocked! TODO: investigate."
        items = extract_suggestions(text)
        kinds = {i.kind for i in items}
        self.assertIn("risk", kinds)
        self.assertIn("task", kinds)

    def test_decision_priority_over_commitment_via_elif(self) -> None:
        # A sentence that matches decision AND contains "i will" — decision
        # branch fires first because of the elif chain.
        text = "Decision: i will approve the PR."
        items = extract_suggestions(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "decision")

    def test_all_sentences_unmatched_returns_single_note(self) -> None:
        text = "Here is some context. Nothing special here. Just notes."
        items = extract_suggestions(text)
        # Fallback fires once on the entire text, not per-sentence.
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "note")
        self.assertAlmostEqual(items[0].confidence, 0.5)


class SuggestedItemDataclassTest(unittest.TestCase):
    """SuggestedItem behaves as a proper dataclass."""

    def test_fields_accessible(self) -> None:
        item = SuggestedItem(kind="task", text="Do something", confidence=0.7)
        self.assertEqual(item.kind, "task")
        self.assertEqual(item.text, "Do something")
        self.assertAlmostEqual(item.confidence, 0.7)

    def test_equality(self) -> None:
        a = SuggestedItem("risk", "Blocked on X", 0.8)
        b = SuggestedItem("risk", "Blocked on X", 0.8)
        self.assertEqual(a, b)

    def test_inequality(self) -> None:
        a = SuggestedItem("risk", "Blocked on X", 0.8)
        b = SuggestedItem("task", "Blocked on X", 0.8)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
