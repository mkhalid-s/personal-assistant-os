"""Unit tests for retrieval.py — primitive functions and embedding backend seam.

Previously retrieval.py had zero direct test coverage despite being the
scoring foundation for both graphrag.retrieve() and planner._agent_analogies().
"""

from __future__ import annotations

import math
import unittest

from personal_assistant.retrieval import (
    EmbeddingBackend,
    _HashBackend,
    cosine_similarity,
    embed_text,
    get_embedding_backend,
    hybrid_score,
    is_semantic_backend,
    lexical_score,
    set_embedding_backend,
    tokenize,
)

# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------


class TokenizeTest(unittest.TestCase):
    def test_lowercases_and_splits(self) -> None:
        self.assertEqual(tokenize("Hello World"), ["hello", "world"])

    def test_strips_punctuation(self) -> None:
        self.assertEqual(tokenize("foo-bar.baz!"), ["foo", "bar", "baz"])

    def test_keeps_digits(self) -> None:
        self.assertIn("42", tokenize("issue #42"))

    def test_empty_string(self) -> None:
        self.assertEqual(tokenize(""), [])

    def test_only_punctuation(self) -> None:
        self.assertEqual(tokenize("!!! ---"), [])


# ---------------------------------------------------------------------------
# lexical_score
# ---------------------------------------------------------------------------


class LexicalScoreTest(unittest.TestCase):
    def test_exact_match_returns_positive(self) -> None:
        self.assertGreater(lexical_score("auth is broken", "auth is broken"), 0.0)

    def test_no_overlap_returns_zero(self) -> None:
        self.assertEqual(lexical_score("unrelated", "completely different text"), 0.0)

    def test_empty_query_returns_zero(self) -> None:
        self.assertEqual(lexical_score("", "some text"), 0.0)

    def test_empty_text_returns_zero(self) -> None:
        self.assertEqual(lexical_score("query", ""), 0.0)

    def test_partial_overlap_between_zero_and_full(self) -> None:
        score = lexical_score("auth service broken", "auth works fine")
        self.assertGreater(score, 0.0)
        self.assertLess(score, lexical_score("auth service broken", "auth service is broken"))

    def test_score_is_non_negative(self) -> None:
        self.assertGreaterEqual(lexical_score("x y z", "a b c"), 0.0)


# ---------------------------------------------------------------------------
# embed_text (hash backend)
# ---------------------------------------------------------------------------


class EmbedTextTest(unittest.TestCase):
    def test_returns_list_of_floats(self) -> None:
        vec = embed_text("hello world")
        self.assertIsInstance(vec, list)
        self.assertTrue(all(isinstance(v, float) for v in vec))

    def test_default_dims_is_64(self) -> None:
        self.assertEqual(len(embed_text("hello")), 64)

    def test_custom_dims(self) -> None:
        self.assertEqual(len(embed_text("hello", dims=128)), 128)

    def test_empty_text_returns_zero_vector(self) -> None:
        vec = embed_text("")
        self.assertEqual(len(vec), 64)
        self.assertEqual(sum(vec), 0.0)

    def test_non_empty_vector_is_unit_length(self) -> None:
        vec = embed_text("some text to embed")
        norm = math.sqrt(sum(v * v for v in vec))
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_deterministic(self) -> None:
        self.assertEqual(embed_text("hello world"), embed_text("hello world"))

    def test_different_texts_differ(self) -> None:
        self.assertNotEqual(embed_text("auth service"), embed_text("deployment pipeline"))


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class CosineSimilarityTest(unittest.TestCase):
    def test_identical_unit_vectors_return_one(self) -> None:
        v = embed_text("hello world")
        self.assertAlmostEqual(cosine_similarity(v, v), 1.0, places=6)

    def test_orthogonal_vectors_return_zero(self) -> None:
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        self.assertAlmostEqual(cosine_similarity(a, b), 0.0, places=9)

    def test_dimension_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])

    def test_result_between_minus_one_and_one(self) -> None:
        a = embed_text("auth service broken")
        b = embed_text("deployment pipeline failed")
        score = cosine_similarity(a, b)
        self.assertGreaterEqual(score, -1.0)
        self.assertLessEqual(score, 1.0)


# ---------------------------------------------------------------------------
# hybrid_score
# ---------------------------------------------------------------------------


class HybridScoreTest(unittest.TestCase):
    def setUp(self) -> None:
        # Reset to hash backend before each test.
        set_embedding_backend(_HashBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())

    def test_identical_texts_return_positive(self) -> None:
        self.assertGreater(hybrid_score("auth is broken", "auth is broken"), 0.0)

    def test_unrelated_texts_lower_than_related(self) -> None:
        related = hybrid_score("auth service", "auth is the service")
        unrelated = hybrid_score("auth service", "deployment pipeline timeout")
        self.assertGreater(related, unrelated)

    def test_custom_weights_respected(self) -> None:
        # With lexical_w=1.0, semantic_w=0.0 the result equals lexical_score.
        q, t = "auth broken", "auth service is broken"
        expected_lex = lexical_score(q, t)
        result = hybrid_score(q, t, lexical_w=1.0, semantic_w=0.0)
        self.assertAlmostEqual(result, expected_lex, places=9)

    def test_default_weights_sum_respected(self) -> None:
        # For a query whose tokens are all unique (no repeats), lexical_score
        # of an identical document is exactly 1.0 and cosine_similarity of
        # unit vectors is 1.0, so hybrid = 0.45*1 + 0.55*1 = 1.0.
        # Note: lexical_score is NOT bounded by 1 when tokens repeat — the
        # Counter accumulates duplicate occurrences, so score > 1 is possible.
        v = "cat dog bird"  # all unique tokens
        score = hybrid_score(v, v)
        self.assertAlmostEqual(score, 1.0, places=5)

    def test_custom_backend_is_called(self) -> None:
        calls: list[str] = []

        class TrackingBackend:
            dims = 64

            def embed(self, text: str) -> list[float]:
                calls.append(text)
                return embed_text(text)

        set_embedding_backend(TrackingBackend())
        hybrid_score("query", "document text")
        self.assertIn("query", calls)
        self.assertIn("document text", calls)


# ---------------------------------------------------------------------------
# EmbeddingBackend seam
# ---------------------------------------------------------------------------


class EmbeddingBackendSeamTest(unittest.TestCase):
    def setUp(self) -> None:
        set_embedding_backend(_HashBackend())

    def tearDown(self) -> None:
        set_embedding_backend(_HashBackend())

    def test_default_backend_is_hash(self) -> None:
        self.assertIsInstance(get_embedding_backend(), _HashBackend)

    def test_is_semantic_backend_false_for_hash(self) -> None:
        self.assertFalse(is_semantic_backend())

    def test_register_custom_backend(self) -> None:
        class MockBackend:
            dims = 384

            def embed(self, text: str) -> list[float]:
                return [0.0] * self.dims

        set_embedding_backend(MockBackend())
        self.assertIsInstance(get_embedding_backend(), MockBackend)
        self.assertTrue(is_semantic_backend())

    def test_protocol_satisfied_by_hash_backend(self) -> None:
        self.assertIsInstance(_HashBackend(), EmbeddingBackend)

    def test_hash_backend_dims(self) -> None:
        self.assertEqual(_HashBackend().dims, 64)

    def test_hash_backend_embed_matches_embed_text(self) -> None:
        b = _HashBackend()
        text = "test embedding content"
        self.assertEqual(b.embed(text), embed_text(text, dims=64))

    def test_reset_to_hash_restores_default(self) -> None:
        class MockBackend:
            dims = 128

            def embed(self, text: str) -> list[float]:
                return [0.0] * self.dims

        set_embedding_backend(MockBackend())
        set_embedding_backend(_HashBackend())
        self.assertFalse(is_semantic_backend())

    def test_backend_dims_propagated_to_embed(self) -> None:
        class Wide:
            dims = 256

            def embed(self, text: str) -> list[float]:
                return embed_text(text, dims=256)

        set_embedding_backend(Wide())
        b = get_embedding_backend()
        self.assertEqual(b.dims, 256)
        self.assertEqual(len(b.embed("hello")), 256)


if __name__ == "__main__":
    unittest.main()
