"""Tests unitaires : filtrage/refus des chunks vides AVANT tout appel embeddings.

Garantit qu'aucun texte vide/blanc n'est transmis à un vrai provider (Gateway/
Ollama/OpenAI). Le stub reste exempt (dev/tests déterministes).
"""
from __future__ import annotations

import pytest

from apps.llm_router import embeddings as emb
from apps.llm_router.embeddings import EmbeddingsError, filter_nonblank_texts, is_blank_text


def test_is_blank_text_variants():
    assert is_blank_text(None)
    assert is_blank_text("")
    assert is_blank_text("   ")
    assert is_blank_text("\n\t")
    assert not is_blank_text("x")


def test_filter_nonblank_texts_removes_blanks():
    assert filter_nonblank_texts(["a", "", "  ", None, "\n\t", "b"]) == ["a", "b"]


def test_embed_texts_rejects_blank_for_real_provider(monkeypatch):
    # Force un provider réel (non-stub) et interdit tout appel réseau.
    monkeypatch.setattr(emb, "_get_setting", _fake_setting(provider="ollama"))

    def _no_net(*a, **k):
        raise AssertionError("no network call must happen when a blank text is present")

    monkeypatch.setattr(emb.requests, "post", _no_net)
    with pytest.raises(EmbeddingsError):
        emb.embed_texts(["valide", "   "], dim=768, provider="ollama", model="nomic-embed-text")


def test_embed_texts_stub_is_exempt(monkeypatch):
    # Le stub global prime : même avec un blanc, pas d'exception (déterministe).
    monkeypatch.setattr(emb, "_get_setting", _fake_setting(provider="stub"))
    vecs = emb.embed_texts(["a", "   "], dim=8)
    assert len(vecs) == 2 and all(len(v) == 8 for v in vecs)


def _fake_setting(*, provider: str):
    defaults = {
        "RAG_EMBEDDING_DIM": 768,
        "EMBEDDINGS_TIMEOUT_S": 30,
        "EMBEDDINGS_PROVIDER": provider,
    }

    def _get(name, default=None):
        return defaults.get(name, default)

    return _get
