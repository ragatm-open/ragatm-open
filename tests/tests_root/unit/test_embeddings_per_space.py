"""Étape 1 — l'indexation utilise les embeddings du RagSpace actif.

Couvre :
- résolution provider/modèle depuis le RagSpace (resolve_space_embedding) ;
- routage de embed_texts vers le bon provider + escape hatch "stub" global ;
- le pipeline d'ingestion passe bien le provider/modèle de l'espace à embed_texts ;
- le provider de CHAT n'influence pas les embeddings.
"""
import pytest

from apps.llm_router import embeddings as emb_mod
from apps.llm_router.embeddings import (
    default_embedding_model_for_provider,
    embed_texts,
    resolve_space_embedding,
)

pytestmark = pytest.mark.django_db


def _gateway_capture(captured: dict):
    """Doublure du chemin gouverné : enregistre modèle/tâche, rend des vecteurs nuls."""
    def _fake(texts, *, model, task, dim, rag_space_id=None, document_id=None):
        captured["provider"] = "ai_gateway"
        captured["model"] = model
        return emb_mod.EmbeddingBatch(
            vectors=[[0.0] * dim for _ in texts], pipeline_id="emb-test", dimension=dim, task=task
        )
    return _fake


def test_default_model_ignores_default_sentinel(settings, monkeypatch):
    """EMBEDDINGS_MODEL='default' (placeholder de base.py) ne doit jamais être
    envoyé à un provider : régression du 404 'model default does not exist'."""
    settings.EMBEDDINGS_MODEL = "default"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "legacy-768"
    assert default_embedding_model_for_provider("ai_gateway") == "legacy-768"


def test_empty_space_model_resolves_to_provider_default(settings, monkeypatch):
    """Un espace gouverné sans modèle explicite -> profil Gateway par défaut (pas 'default')."""
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"  # global != stub
    settings.EMBEDDINGS_MODEL = "default"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "legacy-768"
    captured = {}
    monkeypatch.setattr(emb_mod, "embed_via_gateway", _gateway_capture(captured))
    embed_texts(["x"], dim=4, provider="ai_gateway", model="")  # modèle vide
    assert captured["model"] == "legacy-768"


def test_resolve_space_embedding_reads_space_fields(rag_space):
    rag_space.embedding_provider = "ollama"
    rag_space.embedding_model = "nomic-embed-text"
    rag_space.save(update_fields=["embedding_provider", "embedding_model"])
    provider, model = resolve_space_embedding(rag_space)
    assert provider == "ollama"
    assert model == "nomic-embed-text"


def test_stub_escape_hatch_wins_over_space_provider(settings, monkeypatch):
    """EMBEDDINGS_PROVIDER=stub (CI/dev) force le stub même si l'espace est gouverné."""
    settings.EMBEDDINGS_PROVIDER = "stub"

    def _boom(*a, **k):
        raise AssertionError("ne doit pas appeler le provider réel sous stub")

    monkeypatch.setattr(emb_mod, "embed_via_gateway", _boom)

    vecs = embed_texts(["bonjour"], dim=8, provider="ai_gateway", model="legacy-768")
    assert len(vecs) == 1
    assert len(vecs[0]) == 8


def test_embed_texts_routes_to_passed_provider(settings, monkeypatch):
    """Hors stub, embed_texts route vers le pipeline gouverné avec le modèle passé (celui du
    space) ; tout autre provider est REFUSÉ — aucun chemin direct n'existe en édition Open."""
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"  # global != stub
    captured = {}
    monkeypatch.setattr(emb_mod, "embed_via_gateway", _gateway_capture(captured))

    embed_texts(["x"], dim=4, provider="ai_gateway", model="fr-en-retrieval")
    assert captured == {"provider": "ai_gateway", "model": "fr-en-retrieval"}

    captured.clear()
    with pytest.raises(emb_mod.EmbeddingsError):
        embed_texts(["x"], dim=4, provider="ollama", model="my-embed")
    assert captured == {}


def test_ingestion_passes_space_embedding_provider(rag_space, ingest_text, monkeypatch):
    """Le pipeline sync passe le provider/modèle d'embeddings de l'espace à embed_texts.

    Le provider de CHAT ne doit PAS influencer l'indexation (pipeline gouverné).
    """
    rag_space.llm_provider = "ollama"  # chat local
    rag_space.embedding_provider = "ai_gateway"  # embeddings gouvernés
    rag_space.embedding_model = "fr-en-retrieval"
    rag_space.save(update_fields=["llm_provider", "embedding_provider", "embedding_model"])

    # INGEST-1B : le texte synchrone est indexé par le cœur commun ; le point
    # d'observation est donc `indexing.embed_texts_with_identity` (et non plus le
    # rail direct `pipeline.embed_texts`, supprimé). L'assertion est inchangée.
    from apps.llm_router.embeddings_gateway import EmbeddingBatch
    from apps.rag_core.services.ingest import indexing as indexing_mod

    seen = {}

    def _capture(texts, dim=None, *, provider=None, model=None, **kwargs):
        seen["provider"] = provider
        seen["model"] = model
        d = dim or 768
        return EmbeddingBatch(
            vectors=[[0.0] * d for _ in texts],
            pipeline_id=f"legacy:{provider}:{model}:{d}:legacy-v1", dimension=d, task="document",
        )

    monkeypatch.setattr(indexing_mod, "embed_texts_with_identity", _capture)

    doc_id = ingest_text(rag_space, "Un document de test pour l'indexation.")

    from apps.rag_core.models import Document

    doc = Document.objects.get(id=doc_id)
    assert doc.status == "READY"
    assert seen["provider"] == "ai_gateway"  # embeddings du space, pas le chat
    assert seen["model"] == "fr-en-retrieval"
