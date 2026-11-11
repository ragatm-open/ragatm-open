"""Tests purs de apps.rag_core.domain.policies (should_answer / has_any_signal)."""
import pytest

from apps.rag_core.domain.policies import RagPolicy, has_any_signal, should_answer


def _policy(**kw):
    base = dict(
        min_sources=1,
        min_text_rank=0.05,
        min_score=0.5,
        rerank_enabled=False,
        rerank_top_k=5,
    )
    base.update(kw)
    return RagPolicy(**base)


class TestHasAnySignal:
    def test_empty_is_false(self):
        assert has_any_signal([]) is False

    def test_zero_score_is_false(self):
        assert has_any_signal([{"score": 0.0}]) is False

    def test_missing_score_is_false(self):
        assert has_any_signal([{}]) is False

    def test_positive_score_is_true(self):
        assert has_any_signal([{"score": 0.1}]) is True


class TestShouldAnswer:
    def test_not_enough_sources(self):
        pol = _policy(min_sources=2)
        assert should_answer([{"score": 0.9, "vector_score": 0.9}], pol) is False

    def test_empty_chunks_min_sources_one(self):
        assert should_answer([], _policy(min_sources=1)) is False

    def test_vector_score_below_min_score(self):
        pol = _policy(min_score=0.5, min_text_rank=0.05)
        chunks = [{"score": 0.49, "text_score": 0.0, "vector_score": 0.49}]
        assert should_answer(chunks, pol) is False

    def test_vector_score_at_min_score_inclusive(self):
        pol = _policy(min_score=0.5, min_text_rank=0.05)
        chunks = [{"score": 0.50, "text_score": 0.0, "vector_score": 0.50}]
        assert should_answer(chunks, pol) is True

    def test_fts_signal_takes_priority_and_blocks(self):
        # text_score>0 => on juge sur min_text_rank, le vecteur/score est ignoré
        pol = _policy(min_text_rank=0.05, min_score=0.9)
        chunks = [{"text_score": 0.04, "score": 0.99, "vector_score": 0.99}]
        assert should_answer(chunks, pol) is False

    def test_fts_signal_passes_min_text_rank(self):
        pol = _policy(min_text_rank=0.05, min_score=0.9)
        chunks = [{"text_score": 0.05, "score": 0.01, "vector_score": 0.0}]
        assert should_answer(chunks, pol) is True

    def test_best_any_fallback(self):
        pol = _policy(min_score=0.3, min_text_rank=0.0)
        chunks = [{"score": 0.31, "text_score": 0.0, "vector_score": 0.0}]
        assert should_answer(chunks, pol) is True
