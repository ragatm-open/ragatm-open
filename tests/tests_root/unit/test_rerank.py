"""Tests du rerank (stub déterministe + cross-encoder mocké + fallback)."""
import sys
import types

import pytest

import apps.rag_core.services.rerank as rerank_mod
from apps.rag_core.services.rerank import rerank_chunks


@pytest.fixture(autouse=True)
def _reset_cross_encoder_singletons():
    """Les singletons module-level persistent entre tests -> reset."""
    rerank_mod._CROSS_ENCODER_MODEL = None
    rerank_mod._CROSS_ENCODER_SIGNATURE = None
    yield
    rerank_mod._CROSS_ENCODER_MODEL = None
    rerank_mod._CROSS_ENCODER_SIGNATURE = None


class TestStubRerank:
    def test_never_raises_and_adds_rerank_score(self):
        out = rerank_chunks(question="capitale", chunks=[{"text": "la capitale", "score": 0.1}])
        assert all("rerank_score" in c for c in out)

    def test_question_term_match_boosts_score(self):
        out = rerank_chunks(question="capitale", chunks=[{"text": "la capitale", "score": 0.1}])
        # +0.05 par terme question présent
        assert out[0]["rerank_score"] == pytest.approx(0.15, abs=1e-6)

    def test_ordering_desc(self):
        chunks = [
            {"text": "rien", "score": 0.1},
            {"text": "capitale france", "score": 0.1},
        ]
        out = rerank_chunks(question="capitale france", chunks=chunks)
        assert out[0]["text"] == "capitale france"

    def test_does_not_mutate_input(self):
        original = [{"text": "x", "score": 0.1}]
        rerank_chunks(question="x", chunks=original)
        assert "rerank_score" not in original[0]


class TestCrossEncoder:
    def _install_fake_sentence_transformers(self, monkeypatch, scores):
        class _FakeCrossEncoder:
            def __init__(self, model_name, device=None, **kw):
                self.model_name = model_name

            def predict(self, pairs, batch_size=16, **kw):
                return list(scores)

        fake_mod = types.SimpleNamespace(CrossEncoder=_FakeCrossEncoder)
        monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    def test_cross_encoder_orders_by_predicted_scores(self, monkeypatch, settings):
        settings.RAG_RERANK_PROVIDER = "cross_encoder"
        self._install_fake_sentence_transformers(monkeypatch, [0.1, 0.9, 0.2])
        chunks = [{"text": "a", "score": 0.0}, {"text": "b", "score": 0.0}, {"text": "c", "score": 0.0}]
        out = rerank_chunks(question="q", chunks=chunks)
        assert [c["text"] for c in out] == ["b", "c", "a"]
        assert out[0]["rerank_score"] == pytest.approx(0.9)

    def test_falls_back_to_stub_when_dependency_missing(self, monkeypatch, settings):
        settings.RAG_RERANK_PROVIDER = "cross_encoder"
        monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
        # forcer l'import à échouer
        monkeypatch.setattr(
            rerank_mod, "_get_cross_encoder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no dep"))
        )
        out = rerank_chunks(question="capitale", chunks=[{"text": "la capitale", "score": 0.1}])
        assert all("rerank_score" in c for c in out)
