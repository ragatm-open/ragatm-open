"""Tests de l'observabilité rerank (B4) : RerankInfo + détection du fallback."""
import sys
import types

import pytest
from django.test import override_settings

from apps.rag_core.services import rerank as rerank_mod
from apps.rag_core.services.rerank import rerank_chunks_observed


def _chunks():
    return [
        {"chunk_id": "a", "text": "Paris est la capitale", "score": 0.20},
        {"chunk_id": "b", "text": "Autre contenu", "score": 0.30},
    ]


@override_settings(RAG_RERANK_PROVIDER="stub")
def test_observed_stub_reports_no_fallback():
    reranked, info = rerank_chunks_observed(question="capitale paris", chunks=_chunks())
    assert info.requested_provider == "stub"
    assert info.effective_provider == "stub"
    assert info.fallback_used is False
    assert info.n_in == 2
    assert info.n_out == 2
    assert info.error == ""
    assert all("rerank_score" in c for c in reranked)
    d = info.to_dict()
    assert d["effective_provider"] == "stub" and d["fallback_used"] is False


@override_settings(RAG_RERANK_PROVIDER="cross_encoder", RAG_RERANK_DEVICE="cuda")
def test_observed_cross_encoder_fallback_is_detected(monkeypatch):
    rerank_mod._CROSS_ENCODER_MODEL = None
    rerank_mod._CROSS_ENCODER_SIGNATURE = None
    # Force l'échec de l'import (extra "rerank" non installé en test).
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)

    reranked, info = rerank_chunks_observed(question="capitale paris", chunks=_chunks())
    assert info.requested_provider == "cross_encoder"
    assert info.effective_provider == "stub"
    assert info.fallback_used is True
    assert info.error  # message d'erreur capturé
    assert len(reranked) == 2


@override_settings(
    RAG_RERANK_PROVIDER="cross_encoder",
    RAG_RERANK_MODEL_NAME="fake/model",
    RAG_RERANK_DEVICE="cpu",
)
def test_observed_cross_encoder_success_reports_model(monkeypatch):
    rerank_mod._CROSS_ENCODER_MODEL = None
    rerank_mod._CROSS_ENCODER_SIGNATURE = None
    fake_st = types.SimpleNamespace(
        CrossEncoder=lambda model_name, device=None: types.SimpleNamespace(
            predict=lambda pairs, batch_size=16: [0.1, 0.9]
        )
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    reranked, info = rerank_chunks_observed(question="q", chunks=_chunks())
    assert info.effective_provider == "cross_encoder"
    assert info.fallback_used is False
    assert info.model == "fake/model"
    assert [c["chunk_id"] for c in reranked] == ["b", "a"]
