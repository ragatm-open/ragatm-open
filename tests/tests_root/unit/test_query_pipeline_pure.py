"""Tests purs des étapes de pipeline retrieval : hygiene, merge, diversify."""
import pytest
from django.test import override_settings

from apps.rag_core.services.query.diversify import diversify_by_document
from apps.rag_core.services.query.hygiene import clean_candidates
from apps.rag_core.services.query.merge import merge_candidates, merge_hybrid


class TestCleanCandidates:
    def test_none_and_empty(self):
        assert clean_candidates(None) == []
        assert clean_candidates([]) == []

    @override_settings(RAG_MIN_SCORE=0.0)
    def test_strips_text_and_keeps_id(self):
        out = clean_candidates([{"chunk_id": "a", "score": 0.5, "text": " x "}])
        assert out[0]["text"] == "x"

    @override_settings(RAG_MIN_SCORE=0.0)
    def test_drops_chunk_without_id(self):
        assert clean_candidates([{"score": 0.9, "text": "y"}]) == []

    @override_settings(RAG_MIN_SCORE=0.0)
    def test_dedup_keeps_best_score(self):
        out = clean_candidates(
            [
                {"chunk_id": "a", "score": 0.2, "text": "x"},
                {"chunk_id": "a", "score": 0.8, "text": "x"},
            ]
        )
        assert len(out) == 1
        assert out[0]["score"] == 0.8

    @override_settings(RAG_MIN_SCORE=0.5)
    def test_drops_below_min_score(self):
        assert clean_candidates([{"chunk_id": "a", "score": 0.4, "text": "x"}]) == []


class TestMergeHybrid:
    def test_combines_text_and_vector_scores(self):
        out = merge_hybrid(
            text_chunks=[{"chunk_id": "a", "score": 0.3}],
            vector_chunks=[{"chunk_id": "a", "score": 0.8}],
            top_k=10,
        )
        assert len(out) == 1
        assert out[0]["text_score"] == 0.3
        assert out[0]["vector_score"] == 0.8
        assert out[0]["score"] == 0.8

    def test_vector_only_chunk_has_zero_text_score(self):
        out = merge_hybrid(
            text_chunks=[],
            vector_chunks=[{"chunk_id": "b", "score": 0.7}],
            top_k=10,
        )
        assert out[0]["text_score"] == 0.0


class TestMergeCandidates:
    def test_keeps_best_and_merges_fields(self):
        out = merge_candidates(
            a=[{"chunk_id": "a", "score": 0.2, "x": 1}],
            b=[{"chunk_id": "a", "score": 0.9, "y": 2}],
            top_k=10,
        )
        assert len(out) == 1
        assert out[0]["score"] == 0.9
        assert out[0].get("y") == 2

    def test_top_k_limits(self):
        chunks = [{"chunk_id": str(i), "score": i / 10} for i in range(3)]
        out = merge_candidates(a=chunks, b=[], top_k=1)
        assert len(out) == 1


class TestDiversifyByDocument:
    def test_empty(self):
        assert diversify_by_document([], top_k=5, min_unique_docs=2) == []

    def test_prefers_distinct_documents_then_fills(self):
        chunks = [
            {"chunk_id": "1", "source": {"document_id": "d1"}},
            {"chunk_id": "2", "source": {"document_id": "d1"}},
            {"chunk_id": "3", "source": {"document_id": "d2"}},
        ]
        out = diversify_by_document(chunks, top_k=3, min_unique_docs=2)
        docs = [c["source"]["document_id"] for c in out]
        assert "d1" in docs and "d2" in docs
        assert len(out) == 3
