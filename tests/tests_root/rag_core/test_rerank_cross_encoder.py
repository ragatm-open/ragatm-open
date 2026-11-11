import types

import pytest
from django.test import override_settings


class _FakeCrossEncoder:
    def __init__(self, scores):
        self._scores = scores

    def predict(self, pairs, batch_size=16):
        # "pairs" is a list[(question, chunk_text)]
        assert len(pairs) == len(self._scores)
        return self._scores


@override_settings(
    RAG_RERANK_PROVIDER="cross_encoder",
    RAG_RERANK_MODEL_NAME="fake/model",
    RAG_RERANK_DEVICE="cuda",
    RAG_RERANK_BATCH_SIZE=8,
    RAG_RERANK_MAX_TEXT_CHARS=50,
)
def test_rerank_cross_encoder_orders_by_predict_scores(monkeypatch):
    """Unit test: rerank_chunks() uses cross-encoder scores when provider=cross_encoder."""
    from apps.rag_core.services import rerank as rerank_mod

    # Reset singleton between tests
    rerank_mod._CROSS_ENCODER_MODEL = None
    rerank_mod._CROSS_ENCODER_SIGNATURE = None

    # Inject a fake sentence_transformers module so tests don't require the extra dependency.
    fake_st = types.SimpleNamespace(
        CrossEncoder=lambda model_name, device=None: _FakeCrossEncoder([0.1, 0.9, 0.2])
    )
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)

    chunks = [
        {"chunk_id": "a", "text": "alpha", "score": 0.99},
        {"chunk_id": "b", "text": "bravo", "score": 0.10},
        {"chunk_id": "c", "text": "charlie", "score": 0.50},
    ]

    out = rerank_mod.rerank_chunks(question="q", chunks=chunks)

    assert [c["chunk_id"] for c in out] == ["b", "c", "a"]
    assert out[0]["rerank_score"] == pytest.approx(0.9)


@override_settings(
    RAG_RERANK_PROVIDER="cross_encoder",
    RAG_RERANK_DEVICE="cuda",
)
def test_rerank_cross_encoder_falls_back_to_stub_when_dependency_missing(monkeypatch):
    """Unit test: if sentence_transformers isn't available, we must fallback without crashing."""
    from apps.rag_core.services import rerank as rerank_mod

    rerank_mod._CROSS_ENCODER_MODEL = None
    rerank_mod._CROSS_ENCODER_SIGNATURE = None

    # Ensure import fails
    monkeypatch.delitem(__import__("sys").modules, "sentence_transformers", raising=False)

    chunks = [
        {"chunk_id": "a", "text": "Paris est la capitale", "score": 0.20},
        {"chunk_id": "b", "text": "Autre contenu", "score": 0.30},
    ]

    out = rerank_mod.rerank_chunks(question="capitale paris", chunks=chunks)

    assert len(out) == 2
    assert all("rerank_score" in c for c in out)
