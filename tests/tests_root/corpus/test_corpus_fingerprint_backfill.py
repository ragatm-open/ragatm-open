"""Backfill des empreintes d'indexation — ce que la commande refuse d'inventer.

La migration n'écrit aucune empreinte, délibérément : attribuer aux documents
historiques l'identité déclarée aujourd'hui transformerait une ignorance en certitude.
Ce fichier verrouille la seule chose qui compte ici — la commande n'écrit que sur
preuve, et distingue « je sais que c'est faux » de « je ne sais pas ».
"""
from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusAdmissionCalibration,
    DomainCorpusChunk,
    DomainCorpusDocument,
)
from apps.corpus.services.ingest import ingest_corpus_document_from_upload

pytestmark = pytest.mark.django_db

DIM = 768


@pytest.fixture
def corpus(db):
    domain = BusinessDomain.objects.create(slug="bf", name="Backfill")
    return DomainCorpus.objects.create(
        domain=domain, slug="bf", name="Backfill",
        embedding_provider="ai_gateway", embedding_model="legacy-768",
    )


def _ingest(corpus, *, filename="doc.txt", body=b"Venture capital is equity financing."):
    return ingest_corpus_document_from_upload(
        corpus=corpus, filename=filename, content_type="text/plain", file_bytes=body,
    )


def _forget_fingerprints(corpus):
    """Ramène les documents à l'état « historique » : vecteurs présents, provenance vide."""
    DomainCorpusDocument.objects.filter(corpus=corpus).update(
        embedding_provider="", embedding_model="", embedding_dimension=None,
        embedding_fingerprint="", reindex_required=False,
    )


def _run(*args) -> str:
    from io import StringIO

    out = StringIO()
    call_command("backfill_corpus_embedding_fingerprints", *args, stdout=out)
    return out.getvalue()


# ===========================================================================
# 32-33. Aucune écriture sans ordre explicite
# ===========================================================================
def test_inventory_writes_nothing(corpus):
    _ingest(corpus)
    _forget_fingerprints(corpus)
    out = _run("--inventory")
    assert "fingerprint_missing" in out
    assert DomainCorpusDocument.objects.get(corpus=corpus).embedding_fingerprint == ""


def test_dry_run_measures_but_writes_nothing(corpus):
    _ingest(corpus)
    _forget_fingerprints(corpus)
    out = _run("--corpus", "bf", "--dry-run")
    assert "proven_consistent" in out
    assert "Aucune écriture" in out
    doc = DomainCorpusDocument.objects.get(corpus=corpus)
    assert doc.embedding_fingerprint == ""
    assert doc.reindex_required is False


# ===========================================================================
# 34-35. Preuve suffisante / insuffisante
# ===========================================================================
def test_proven_document_receives_its_fingerprint(corpus):
    from apps.corpus.domain.embedding_identity import EmbeddingIdentity

    _ingest(corpus)
    _forget_fingerprints(corpus)
    _run("--corpus", "bf", "--apply")

    doc = DomainCorpusDocument.objects.get(corpus=corpus)
    expected = EmbeddingIdentity(
        provider="ai_gateway", model="legacy-768", dimension=DIM
    ).fingerprint
    assert doc.embedding_fingerprint == expected
    assert (doc.embedding_provider, doc.embedding_model) == ("ai_gateway", "legacy-768")
    assert doc.embedding_dimension == DIM
    assert doc.reindex_required is False


def test_unprovable_document_is_marked_not_invented(corpus, monkeypatch):
    """Provider injoignable : on ne sait pas, donc on ne conclut pas."""
    from apps.corpus.management.commands import backfill_corpus_embedding_fingerprints as cmd

    _ingest(corpus)
    _forget_fingerprints(corpus)
    import apps.llm_router.embeddings as emb_module

    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider injoignable")),
    )
    out = _run("--corpus", "bf", "--apply")
    assert cmd.UNKNOWN in out
    doc = DomainCorpusDocument.objects.get(corpus=corpus)
    assert doc.embedding_fingerprint == "", "aucune empreinte inventée"
    assert doc.reindex_required is True


def test_dimension_divergence_is_impossible_at_the_column_level(corpus):
    """La colonne pgvector refuse elle-même une dimension divergente.

    Le contrôle de dimension de la commande est donc une ceinture, pas la bretelle :
    tant que `domain_corpus_chunk.embedding` est `vector(768)`, aucun vecteur d'une
    autre dimension ne peut exister. On le documente ici plutôt que de simuler un état
    que la base rend inatteignable.
    """
    from django.db.utils import DataError

    _ingest(corpus)
    chunk = DomainCorpusChunk.objects.filter(corpus=corpus).first()
    with pytest.raises(DataError, match="768"):
        DomainCorpusChunk.objects.filter(id=chunk.id).update(embedding=[0.0] * 8)


def test_wrong_model_is_detected_by_the_cosine_proof(corpus, monkeypatch):
    """Vecteurs stockés produits par un AUTRE modèle : le cosinus le dit."""
    from apps.corpus.management.commands import backfill_corpus_embedding_fingerprints as cmd

    _ingest(corpus)
    _forget_fingerprints(corpus)
    import apps.llm_router.embeddings as emb_module

    real = emb_module.embed_texts
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, *a, **k: [[-x for x in v] for v in real(texts, *a, **k)],
    )
    out = _run("--corpus", "bf", "--apply")
    assert cmd.PROVEN_INCONSISTENT in out
    assert DomainCorpusDocument.objects.get(corpus=corpus).embedding_fingerprint == ""


# ===========================================================================
# 36-39. Idempotence et innocuité
# ===========================================================================
def test_backfill_is_idempotent(corpus):
    _ingest(corpus)
    _forget_fingerprints(corpus)
    _run("--corpus", "bf", "--apply")
    first = DomainCorpusDocument.objects.get(corpus=corpus).embedding_fingerprint
    _run("--corpus", "bf", "--apply")
    assert DomainCorpusDocument.objects.get(corpus=corpus).embedding_fingerprint == first


def test_backfill_touches_no_vector_no_chunk_no_calibration(corpus):
    _ingest(corpus)
    calibration = DomainCorpusAdmissionCalibration.objects.create(
        corpus=corpus, version=1, is_active=True, signal="corpus_vector_top10_mean",
        calibrator_type="logistic", embedding_provider="ai_gateway",
        embedding_model="legacy-768", embedding_dimension=DIM,
        coefficients={"intercept": -1.0, "top10_mean": 2.0}, training={}, validation={},
        dataset_hash="b" * 64,
    )
    _forget_fingerprints(corpus)
    before = {
        c.id: list(c.embedding)
        for c in DomainCorpusChunk.objects.filter(corpus=corpus)
    }
    _run("--corpus", "bf", "--apply")

    after = {c.id: list(c.embedding) for c in DomainCorpusChunk.objects.filter(corpus=corpus)}
    assert before.keys() == after.keys(), "aucun chunk supprimé ni créé"
    assert all(before[k] == after[k] for k in before), "aucun embedding modifié"
    calibration.refresh_from_db()
    assert calibration.is_active is True, "la calibration n'est pas touchée par le backfill"


def test_apply_and_dry_run_are_exclusive(corpus):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="exclusifs"):
        _run("--corpus", "bf", "--apply", "--dry-run")


def test_unknown_corpus_is_refused():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="introuvable"):
        _run("--corpus", "inexistant", "--dry-run")


def test_corpus_without_declared_identity_is_left_alone(db):
    """Sans identité déclarée, il n'y a rien à prouver : on ne devine pas."""
    domain = BusinessDomain.objects.create(slug="sans", name="Sans")
    corpus = DomainCorpus.objects.create(
        domain=domain, slug="sans", name="Sans",
        embedding_provider="ai_gateway", embedding_model="legacy-768",
    )
    _ingest(corpus)
    DomainCorpus.objects.filter(id=corpus.id).update(embedding_model="")
    _forget_fingerprints(corpus)

    out = _run("--corpus", "sans", "--apply")
    assert "identité déclarée inutilisable" in out
    assert DomainCorpusDocument.objects.get(corpus=corpus).embedding_fingerprint == ""


# ===========================================================================
# 49-50. Non-régression : un corpus prouvé reste pleinement disponible
# ===========================================================================
def test_proven_corpus_stays_available_under_enforcement(corpus):
    from django.test import override_settings

    from apps.corpus.domain.embedding_identity import CONSISTENCY_CONSISTENT
    from apps.corpus.services.embedding_consistency import consistency_report

    _ingest(corpus)
    _forget_fingerprints(corpus)
    assert consistency_report(corpus).status != CONSISTENCY_CONSISTENT

    _run("--corpus", "bf", "--apply")
    report = consistency_report(corpus)
    assert report.status == CONSISTENCY_CONSISTENT
    assert report.vector_retrieval_available is True

    with override_settings(
        RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED=True, RAG_MIN_SCORE=0.0
    ):
        from apps.corpus.services.retrieval import search_corpus_chunks_vector
        from apps.llm_router.embeddings import embed_texts

        from apps.corpus.domain.embedding_identity import EmbeddingIdentity

        identity = EmbeddingIdentity(
            provider="ai_gateway", model="legacy-768", dimension=DIM
        )
        emb = embed_texts(["venture capital"], dim=DIM,
                          provider="ai_gateway", model="legacy-768")[0]
        rows = search_corpus_chunks_vector(
            corpus_ids=[str(corpus.id)], query_embedding=emb, top_k=10,
            embedding_fingerprint=identity.fingerprint,
            embedding_provider="ai_gateway", embedding_model="legacy-768",
            embedding_dimension=DIM,
        )
        assert rows, "un corpus prouvé reste interrogeable sous filtrage strict"
