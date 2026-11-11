"""Résolution du modèle d'embeddings d'un corpus — refus des replis silencieux.

Le défaut corrigé ici a coûté une nuit de diagnostic. Un corpus dont
``embedding_model`` est vide voyait son modèle résolu, à l'exécution, par une cascade de
variables GLOBALES (``OPENAI_EMBEDDINGS_MODEL``, puis ``EMBEDDINGS_MODEL``, puis un
défaut de code) qui ne le concernent pas.

Mesuré entre deux environnements portant les MÊMES données : le leg vectoriel du corpus
rendait 120 candidats d'un côté, 0 de l'autre. Les trois autres legs étaient identiques
chunk par chunk. La production ne fonctionnait que parce qu'une variable globale se
trouvait posée ; l'environnement où elle manquait envoyait ``nomic-embed-text`` à
OpenAI, recevait un 404, et dégradait le leg **sans un mot**.

Le pire cas n'a pas été observé mais reste permis par une cascade : un modèle VALIDE
mais différent de celui qui a produit les vecteurs encoderait la question dans un espace
incomparable. Le leg répondrait — avec des résultats plausibles et faux.

D'où la règle testée ici : la provenance des vecteurs est portée par le corpus. Sans
modèle explicite, on refuse AVANT tout calcul, à l'indexation comme au retrieval.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.corpus.models import BusinessDomain, DomainCorpus
from apps.corpus.services.ingest import resolve_corpus_embedding
from apps.corpus.services.retrieval import (
    CORPUS_EMBEDDING_MODEL_MISSING,
    CorpusEmbeddingConfigurationError,
    _group_corpus_ids_by_embedding,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def domain(db):
    return BusinessDomain.objects.create(slug="emb", name="Embeddings")


def _corpus(domain, *, provider: str, model: str, slug: str) -> DomainCorpus:
    return DomainCorpus.objects.create(
        domain=domain, slug=slug, name=slug,
        embedding_provider=provider, embedding_model=model,
    )


# ===========================================================================
# Les quatre cas de configuration
# ===========================================================================
def test_explicit_provider_and_model_succeeds(domain):
    corpus = _corpus(domain, provider="ai_gateway", model="legacy-768", slug="ok")
    assert resolve_corpus_embedding(corpus) == ("ai_gateway", "legacy-768")
    assert _group_corpus_ids_by_embedding([str(corpus.id)]) == [
        ("ai_gateway", "legacy-768", [str(corpus.id)])
    ]


def test_explicit_provider_without_model_is_refused(domain):
    """Le cas qui a produit l'incident : provider posé, modèle vide."""
    corpus = _corpus(domain, provider="ai_gateway", model="", slug="sans-modele")

    with pytest.raises(CorpusEmbeddingConfigurationError) as at_ingest:
        resolve_corpus_embedding(corpus)
    assert at_ingest.value.code == CORPUS_EMBEDDING_MODEL_MISSING

    with pytest.raises(CorpusEmbeddingConfigurationError) as at_retrieval:
        _group_corpus_ids_by_embedding([str(corpus.id)])
    assert at_retrieval.value.code == CORPUS_EMBEDDING_MODEL_MISSING
    assert at_retrieval.value.corpus_id == str(corpus.id)
    assert at_retrieval.value.provider == "ai_gateway"


def test_no_provider_and_no_model_is_an_explicit_unconfigured_state(domain):
    """Sans provider, le corpus n'est pas « mal configuré » : il n'est pas vectorisé.

    Le repli sur le réglage global reste légitime ici — c'est le cas des très anciens
    corpus non migrés, et il ne prétend rien sur la provenance des vecteurs.
    """
    corpus = _corpus(domain, provider="", model="", slug="non-configure")
    assert resolve_corpus_embedding(corpus) == (None, None)
    assert _group_corpus_ids_by_embedding([str(corpus.id)]) == [(None, None, [str(corpus.id)])]


def test_error_message_carries_no_secret_or_document_content(domain):
    corpus = _corpus(domain, provider="ai_gateway", model="", slug="fuite")
    with pytest.raises(CorpusEmbeddingConfigurationError) as exc:
        _group_corpus_ids_by_embedding([str(corpus.id)])
    message = str(exc.value)
    assert CORPUS_EMBEDDING_MODEL_MISSING in message
    assert "sk-" not in message and "Bearer" not in message


# ===========================================================================
# Non-régression : la variable globale ne doit plus décider
# ===========================================================================
@override_settings(
    OPENAI_EMBEDDINGS_MODEL="legacy-768",
    EMBEDDINGS_MODEL="legacy-768",
)
def test_global_setting_no_longer_rescues_a_corpus_without_model(domain):
    """Le cœur du correctif.

    Avec l'ancien code, ce corpus « fonctionnait » — parce qu'une variable globale
    était posée. C'est exactement cette dépendance implicite qui rendait un
    environnement vert et l'autre muet, à données identiques.
    """
    corpus = _corpus(domain, provider="ai_gateway", model="", slug="globale")
    with pytest.raises(CorpusEmbeddingConfigurationError):
        _group_corpus_ids_by_embedding([str(corpus.id)])


@override_settings(EMBEDDINGS_MODEL="legacy-768")
def test_a_valid_but_wrong_global_model_is_refused_before_any_call(domain):
    """Le pire cas : un modèle global VALIDE mais étranger aux vecteurs stockés.

    Il ne doit produire ni appel, ni résultat — un leg qui répond dans un espace
    vectoriel incomparable est plus nuisible qu'un leg vide, parce qu'il est crédible.
    """
    corpus = _corpus(domain, provider="ai_gateway", model="", slug="mauvais-modele")
    with pytest.raises(CorpusEmbeddingConfigurationError):
        _group_corpus_ids_by_embedding([str(corpus.id)])


# ===========================================================================
# Le leg lexical survit, et la trace NOMME l'erreur
# ===========================================================================
def test_lexical_leg_survives_and_trace_names_the_error(domain, ingest_text, rag_space):
    """Une configuration vectorielle invalide ne doit pas emporter le leg lexical."""
    from apps.corpus.models import WorkspaceDomainCorpusAccess
    from apps.corpus.services.ingest import ingest_corpus_document_from_upload
    from apps.corpus.services.retrieval import retrieve_corpus_candidate_legs
    from apps.rag_core.services.query.trace import CorpusLegRecorder

    corpus = _corpus(domain, provider="ai_gateway", model="legacy-768", slug="mixte")
    ingest_corpus_document_from_upload(
        corpus=corpus, filename="ref.txt", content_type="text/plain",
        file_bytes=b"Venture capital is equity financing for unlisted companies.",
    )
    # Le modèle est retiré APRÈS indexation : les vecteurs existent, leur provenance
    # déclarée a disparu. C'est la situation exacte rencontrée en production.
    DomainCorpus.objects.filter(id=corpus.id).update(embedding_model="")
    WorkspaceDomainCorpusAccess.objects.create(workspace=rag_space.workspace, corpus=corpus)

    recorder = CorpusLegRecorder()
    legs = retrieve_corpus_candidate_legs(
        workspace_id=rag_space.workspace_id, queries=["venture capital"],
        top_k=10, vector_top_k=10, hybrid_enabled=True, min_text_rank=0.0,
        recorder=recorder,
    )
    assert legs.text, "le leg lexical reste exploitable"
    assert legs.vector == [], "le leg vectoriel est refusé, pas deviné"
    assert recorder.vector_error_code == CORPUS_EMBEDDING_MODEL_MISSING
