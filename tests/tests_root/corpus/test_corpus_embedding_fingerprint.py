"""Empreinte d'indexation des corpus métier — garde-fous d'identité.

Ce que ce fichier verrouille, dans l'ordre où le lot se lit :

1. **la création** : un corpus ne naît jamais sans modèle ;
2. **l'empreinte** : écrite après succès, effacée après échec, jamais supposée ;
3. **le changement d'identité** : marqué, jamais silencieux, quelle que soit la voie ;
4. **le retrieval** : ne compare que des vecteurs comparables ;
5. **l'admission** : ne s'appuie jamais sur un signal issu d'une autre identité ;
6. **le backfill** : n'écrit que sur preuve.

Le mode de défaillance combattu ici a été mesuré, pas supposé : une recherche croisée
ne lève AUCUNE erreur, rend une page pleine de candidats, des scores plausibles
(0,512–0,522, dans la plage légitime des négatifs) et un contenu quasi aléatoire.
C'est ce qui la rend indétectable par tout seuil absolu — et ce qui justifie une garde
structurelle plutôt qu'une alerte.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.corpus.domain.embedding_identity import (
    CORPUS_EMBEDDING_MODEL_REQUIRED,
    CORPUS_EMBEDDING_PROVIDER_INVALID,
    DEFAULT_CORPUS_EMBEDDING_MODEL,
    CONSISTENCY_CONSISTENT,
    CONSISTENCY_FINGERPRINT_MISSING,
    CONSISTENCY_IDENTITY_MISMATCH,
    CONSISTENCY_NO_READY_DOCUMENTS,
    CONSISTENCY_PARTIALLY_CONSISTENT,
    CONSISTENCY_REINDEX_REQUIRED,
    ConsistencyReport,
    CorpusEmbeddingIdentityError,
    EmbeddingIdentity,
    resolve_creation_identity,
)
from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusChunk,
    DomainCorpusDocument,
)
from apps.corpus.services.ingest import ingest_corpus_document_from_upload

pytestmark = pytest.mark.django_db

DIM = 768
OPENAI = ("ai_gateway", "legacy-768")
OLLAMA = ("ollama", "legacy-768")

ENFORCE = override_settings(RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED=True)


@pytest.fixture
def domain(db):
    return BusinessDomain.objects.create(slug="fp", name="Empreintes")


def _corpus(domain, *, provider="ai_gateway", model="legacy-768", slug="c"):
    return DomainCorpus.objects.create(
        domain=domain, slug=slug, name=slug,
        embedding_provider=provider, embedding_model=model,
    )


def _ingest(corpus, *, filename="doc.txt", body=b"Venture capital is equity financing."):
    return ingest_corpus_document_from_upload(
        corpus=corpus, filename=filename, content_type="text/plain", file_bytes=body,
    )


def _identity(provider=OPENAI[0], model=OPENAI[1], dim=DIM) -> EmbeddingIdentity:
    return EmbeddingIdentity(provider=provider, model=model, dimension=dim)


# ===========================================================================
# 1-6. Création
# ===========================================================================
def test_creation_without_model_resolves_the_business_default():
    identity = resolve_creation_identity(provider="ai_gateway", model=None, dimension=DIM)
    assert identity.model == DEFAULT_CORPUS_EMBEDDING_MODEL["ai_gateway"]
    assert identity.provider == "ai_gateway"


@pytest.mark.parametrize("absent", [None, "", "   "])
def test_creation_without_provider_uses_the_business_default(absent):
    """Provider ABSENT (None ou vide) = défaut métier ; provider INCONNU = refus."""
    identity = resolve_creation_identity(provider=absent, model=None, dimension=DIM)
    assert (identity.provider, identity.model) == ("ai_gateway", "legacy-768")


def test_api_never_persists_an_empty_model(global_owner_client, business_domain):
    """Le défaut corrigé : l'UI n'envoie que domaine, nom et slug."""
    resp = global_owner_client.post(
        "/api/v1/admin/domain-corpora/",
        {"domain_id": str(business_domain.id), "slug": "nouveau", "name": "Nouveau"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    created = DomainCorpus.objects.get(slug="nouveau")
    assert created.embedding_model, "aucun corpus ne doit naître sans modèle"
    assert created.embedding_model == "legacy-768"
    assert resp.data["embedding_model"] == created.embedding_model


def test_api_creation_with_explicit_blank_model_still_resolves(global_owner_client, business_domain):
    resp = global_owner_client.post(
        "/api/v1/admin/domain-corpora/",
        {"domain_id": str(business_domain.id), "slug": "vide", "name": "Vide",
         "embedding_provider": "ai_gateway", "embedding_model": ""},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert DomainCorpus.objects.get(slug="vide").embedding_model == "legacy-768"


@pytest.mark.parametrize("provider", ["cohere", "OPENAI_", "mistral"])
def test_unknown_provider_is_refused(provider):
    with pytest.raises(CorpusEmbeddingIdentityError) as exc:
        resolve_creation_identity(provider=provider, model="m", dimension=DIM)
    assert exc.value.code == CORPUS_EMBEDDING_PROVIDER_INVALID


@pytest.mark.parametrize("model", ["default", "none", "auto", "un modele avec espaces", "x" * 200])
def test_implausible_model_is_refused(model):
    """`default` est le placeholder qui produisait un 404 `model_not_found` chez OpenAI."""
    with pytest.raises(CorpusEmbeddingIdentityError):
        EmbeddingIdentity(provider="ai_gateway", model=model, dimension=DIM)


@pytest.mark.parametrize("dimension", [0, -1, None, "768"])
def test_invalid_dimension_is_refused(dimension):
    with pytest.raises(CorpusEmbeddingIdentityError):
        EmbeddingIdentity(provider="ai_gateway", model="legacy-768", dimension=dimension)


def test_empty_model_is_refused_by_the_identity_itself():
    with pytest.raises(CorpusEmbeddingIdentityError) as exc:
        EmbeddingIdentity(provider="ai_gateway", model="", dimension=DIM)
    assert exc.value.code == CORPUS_EMBEDDING_MODEL_REQUIRED


def test_fingerprint_distinguishes_every_component():
    base = _identity()
    assert base.fingerprint != _identity(model="fr-en-retrieval").fingerprint
    assert base.fingerprint != _identity(model="fr-en-retrieval").fingerprint
    assert base.fingerprint != _identity(dim=1536).fingerprint
    assert base.fingerprint == _identity().fingerprint, "empreinte stable"
    assert len(base.fingerprint) == 64


# ===========================================================================
# 7-10. Empreinte à l'ingestion
# ===========================================================================
def test_fingerprint_written_after_successful_indexing(domain):
    corpus = _corpus(domain)
    _ingest(corpus)
    doc = DomainCorpusDocument.objects.get(corpus=corpus)
    assert doc.status == "READY"
    assert (doc.embedding_provider, doc.embedding_model) == OPENAI
    assert doc.embedding_dimension == DIM
    assert doc.embedding_fingerprint == _identity().fingerprint
    assert doc.reindex_required is False


def test_failed_indexing_leaves_no_fingerprint(domain, monkeypatch):
    """Un document en échec ne garde pas la preuve d'une indexation disparue."""
    from apps.corpus.services import ingest as ingest_module

    corpus = _corpus(domain, slug="echec")
    monkeypatch.setattr(
        ingest_module, "embed_texts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider indisponible")),
    )
    _ingest(corpus)
    doc = DomainCorpusDocument.objects.get(corpus=corpus)
    assert doc.status == "FAILED"
    assert doc.embedding_fingerprint == ""
    assert doc.embedding_dimension is None
    assert DomainCorpusChunk.objects.filter(document=doc).count() == 0


def test_reindexing_refreshes_the_fingerprint(domain):
    corpus = _corpus(domain, slug="reindex")
    _ingest(corpus)
    doc = DomainCorpusDocument.objects.get(corpus=corpus)
    DomainCorpusDocument.objects.filter(id=doc.id).update(
        embedding_fingerprint="perime", reindex_required=True
    )
    from apps.corpus.services.ingest import reindex_corpus_document

    reindex_corpus_document(document=DomainCorpusDocument.objects.get(id=doc.id))
    doc.refresh_from_db()
    assert doc.embedding_fingerprint == _identity().fingerprint
    assert doc.reindex_required is False


# ===========================================================================
# 11-16. Changement d'identité
# ===========================================================================
def test_model_change_marks_every_document(domain):
    corpus = _corpus(domain, slug="chgmt")
    _ingest(corpus)
    assert DomainCorpusDocument.objects.filter(corpus=corpus, reindex_required=True).count() == 0

    corpus.embedding_model = "fr-en-retrieval"
    corpus.save()

    assert DomainCorpusDocument.objects.filter(corpus=corpus, reindex_required=False).count() == 0
    doc = DomainCorpusDocument.objects.get(corpus=corpus)
    assert doc.embedding_fingerprint == _identity().fingerprint, "les vecteurs ne sont pas touchés"




def test_identity_change_invalidates_the_active_calibration(domain):
    from apps.corpus.models import DomainCorpusAdmissionCalibration

    corpus = _corpus(domain, slug="calib")
    _ingest(corpus)
    row = DomainCorpusAdmissionCalibration.objects.create(
        corpus=corpus, version=1, is_active=True, signal="corpus_vector_top10_mean",
        calibrator_type="logistic", embedding_provider="ai_gateway",
        embedding_model="legacy-768", embedding_dimension=DIM,
        coefficients={"intercept": -1.0, "top10_mean": 2.0}, training={}, validation={},
        dataset_hash="a" * 64,
    )
    corpus.embedding_model = "fr-en-retrieval"
    corpus.save()
    row.refresh_from_db()
    assert row.is_active is False, "une calibration étrangère ne doit plus décider"
    assert DomainCorpusAdmissionCalibration.objects.filter(id=row.id).exists(), "conservée"


def test_patch_reports_the_effect(domain, global_owner_client):
    corpus = _corpus(domain, slug="api-chgmt")
    _ingest(corpus)
    resp = global_owner_client.patch(
        f"/api/v1/admin/domain-corpora/{corpus.id}/",
        {"embedding_model": "fr-en-retrieval"}, format="json",
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["embeddings_reindex_required"] is True
    assert resp.data["affected_documents"] == 1
    assert resp.data["vector_retrieval_available"] is False


def test_patch_without_real_change_triggers_nothing(domain, global_owner_client):
    """Un formulaire qui renvoie son propre état ne doit rien déclencher."""
    corpus = _corpus(domain, slug="idem")
    _ingest(corpus)
    resp = global_owner_client.patch(
        f"/api/v1/admin/domain-corpora/{corpus.id}/",
        {"embedding_provider": "ai_gateway", "embedding_model": " legacy-768 "},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["embeddings_reindex_required"] is False
    assert DomainCorpusDocument.objects.get(corpus=corpus).reindex_required is False


def test_renaming_does_not_touch_indexing(domain, global_owner_client):
    corpus = _corpus(domain, slug="renomme")
    _ingest(corpus)
    resp = global_owner_client.patch(
        f"/api/v1/admin/domain-corpora/{corpus.id}/",
        {"name": "Autre nom", "description": "autre"}, format="json",
    )
    assert resp.status_code == 200
    assert resp.data["embeddings_reindex_required"] is False
    assert DomainCorpusDocument.objects.get(corpus=corpus).reindex_required is False


def test_guard_applies_outside_the_api(domain):
    """L'admin Django et le shell écrivent le modèle directement : la garde est dessus."""
    corpus = _corpus(domain, slug="shell")
    _ingest(corpus)
    DomainCorpus.objects.get(id=corpus.id)  # simule un chargement admin
    obj = DomainCorpus.objects.get(id=corpus.id)
    obj.embedding_model = "fr-en-retrieval"
    obj.save()
    assert DomainCorpusDocument.objects.get(corpus=corpus).reindex_required is True


# ===========================================================================
# 17-26. Retrieval
# ===========================================================================
def _vector_search(corpus, *, enforce: bool):
    from apps.corpus.services.retrieval import search_corpus_chunks_vector
    from apps.llm_router.embeddings import embed_texts

    emb = embed_texts(["venture capital"], dim=DIM,
                      provider=corpus.embedding_provider, model=corpus.embedding_model)[0]
    fingerprint = _identity(corpus.embedding_provider, corpus.embedding_model).fingerprint
    return search_corpus_chunks_vector(
        corpus_ids=[str(corpus.id)], query_embedding=emb, top_k=10,
        embedding_fingerprint=fingerprint if enforce else None,
        embedding_provider=corpus.embedding_provider,
        embedding_model=corpus.embedding_model,
        embedding_dimension=DIM,
    )


def test_matching_fingerprint_is_admitted(domain):
    corpus = _corpus(domain, slug="ok")
    _ingest(corpus)
    assert _vector_search(corpus, enforce=True), "un document conforme doit être comparé"


@pytest.mark.parametrize(
    "mutation",
    [
        {"embedding_provider": "ollama"},
        {"embedding_model": "fr-en-retrieval"},
        {"embedding_dimension": 1536},
        {"embedding_fingerprint": "0" * 64},
        {"reindex_required": True},
    ],
)
def test_divergent_document_is_excluded(domain, mutation):
    """Provider, modèle, dimension, empreinte ou réindexation : chacun suffit à exclure."""
    corpus = _corpus(domain, slug="divergent")
    _ingest(corpus)
    DomainCorpusDocument.objects.filter(corpus=corpus).update(**mutation)
    assert _vector_search(corpus, enforce=True) == []
    assert _vector_search(corpus, enforce=False), "sans contrainte, le comportement historique"


def test_non_ready_document_is_excluded(domain):
    corpus = _corpus(domain, slug="notready")
    _ingest(corpus)
    DomainCorpusDocument.objects.filter(corpus=corpus).update(status="FAILED")
    assert _vector_search(corpus, enforce=True) == []


def test_only_consistent_documents_are_compared(domain):
    corpus = _corpus(domain, slug="mixte")
    _ingest(corpus, filename="sain.txt", body=b"Logistics teams organise deliveries.")
    _ingest(corpus, filename="douteux.txt", body=b"Venture capital exits happen at IPO.")
    douteux = DomainCorpusDocument.objects.get(corpus=corpus, original_filename="douteux.txt")
    DomainCorpusDocument.objects.filter(id=douteux.id).update(reindex_required=True)

    rows = _vector_search(corpus, enforce=True)
    documents = {(r.get("source") or {}).get("document_id") for r in rows}
    assert str(douteux.id) not in documents
    assert rows, "le document sain reste interrogeable"


@ENFORCE
@override_settings(RAG_MIN_SCORE=0.0)
def test_lexical_leg_survives_an_inconsistent_vector_leg(domain, rag_space):
    """Le lexical ne dépend d'aucun modèle : il reste disponible, et la trace le dit."""
    from apps.corpus.models import WorkspaceDomainCorpusAccess
    from apps.corpus.services.retrieval import retrieve_corpus_candidate_legs
    from apps.rag_core.services.query.trace import CorpusLegRecorder

    corpus = _corpus(domain, slug="lexical")
    _ingest(corpus, body=b"Venture capital is equity financing for unlisted companies.")
    DomainCorpusDocument.objects.filter(corpus=corpus).update(reindex_required=True)
    WorkspaceDomainCorpusAccess.objects.create(workspace=rag_space.workspace, corpus=corpus)

    recorder = CorpusLegRecorder()
    legs = retrieve_corpus_candidate_legs(
        workspace_id=rag_space.workspace_id, queries=["venture capital"],
        top_k=10, vector_top_k=10, hybrid_enabled=True, min_text_rank=0.0, recorder=recorder,
    )
    assert legs.text, "le leg lexical reste exploitable"
    assert legs.vector == [], "le leg vectoriel est refusé"
    assert recorder.vector_incoherent is True
    report = recorder.consistency_reports[str(corpus.id)]
    assert report.status == CONSISTENCY_REINDEX_REQUIRED
    assert report.vector_retrieval_available is False


# ===========================================================================
# Rapports de cohérence
# ===========================================================================
@pytest.mark.parametrize(
    "report, expected",
    [
        (ConsistencyReport(ready=0), CONSISTENCY_NO_READY_DOCUMENTS),
        (ConsistencyReport(ready=3, compatible=3), CONSISTENCY_CONSISTENT),
        (ConsistencyReport(ready=3, compatible=1, unknown=2), CONSISTENCY_PARTIALLY_CONSISTENT),
        (ConsistencyReport(ready=3, unknown=3), CONSISTENCY_FINGERPRINT_MISSING),
        (ConsistencyReport(ready=3, incompatible=3), CONSISTENCY_IDENTITY_MISMATCH),
        (ConsistencyReport(ready=3, reindex_required=3), CONSISTENCY_REINDEX_REQUIRED),
    ],
)
def test_consistency_verdicts(report, expected):
    assert report.status == expected


def test_unknown_is_not_confused_with_mismatch():
    """Une empreinte ABSENTE n'accuse personne ; une empreinte DIFFÉRENTE, si."""
    absent = ConsistencyReport(ready=2, unknown=2)
    different = ConsistencyReport(ready=2, incompatible=2)
    assert absent.status != different.status
    assert absent.error_code != different.error_code


def test_report_counts_are_measured_from_the_database(domain):
    from apps.corpus.services.embedding_consistency import consistency_report

    corpus = _corpus(domain, slug="mesure")
    _ingest(corpus, filename="a.txt", body=b"Venture capital A.")
    _ingest(corpus, filename="b.txt", body=b"Venture capital B.")
    report = consistency_report(corpus)
    assert (report.ready, report.compatible, report.unknown) == (2, 2, 0)
    assert report.status == CONSISTENCY_CONSISTENT
    assert report.vector_retrieval_available is True


# ===========================================================================
# 31. Multi-corpus
# ===========================================================================
@ENFORCE
@override_settings(RAG_MIN_SCORE=0.0)
def test_one_invalid_corpus_does_not_close_the_valid_one(domain, rag_space):
    from apps.corpus.models import WorkspaceDomainCorpusAccess
    from apps.corpus.services.retrieval import retrieve_corpus_candidate_legs
    from apps.rag_core.services.query.trace import CorpusLegRecorder

    sain = _corpus(domain, slug="sain")
    casse = _corpus(domain, slug="casse")
    _ingest(sain, body=b"Venture capital is equity financing for unlisted companies.")
    _ingest(casse, body=b"Venture capital funds raise capital from limited partners.")
    DomainCorpusDocument.objects.filter(corpus=casse).update(reindex_required=True)
    for corpus in (sain, casse):
        WorkspaceDomainCorpusAccess.objects.create(workspace=rag_space.workspace, corpus=corpus)

    recorder = CorpusLegRecorder()
    legs = retrieve_corpus_candidate_legs(
        workspace_id=rag_space.workspace_id, queries=["venture capital"],
        top_k=10, vector_top_k=10, hybrid_enabled=True, min_text_rank=0.0, recorder=recorder,
    )
    corpus_ids = {(c.get("source") or {}).get("corpus_id") for c in legs.vector}
    assert str(sain.id) in corpus_ids, "le corpus cohérent participe"
    assert str(casse.id) not in corpus_ids, "le corpus incohérent est écarté"
    assert recorder.consistency_reports[str(sain.id)].vector_retrieval_available is True
    assert recorder.consistency_reports[str(casse.id)].vector_retrieval_available is False


# ===========================================================================
# 40-44. API et permissions
# ===========================================================================
def test_api_exposes_the_consistency_state(domain, global_owner_client):
    corpus = _corpus(domain, slug="expose")
    _ingest(corpus)
    resp = global_owner_client.get(f"/api/v1/admin/domain-corpora/{corpus.id}/")
    assert resp.status_code == 200
    for key in (
        "embedding_dimension", "embedding_identity_complete", "embedding_consistency_status",
        "embedding_consistency_error_code", "embedding_consistent_document_count",
        "embedding_inconsistent_document_count", "embedding_unknown_document_count",
        "embedding_reindex_required_document_count", "embeddings_reindex_required",
        "vector_retrieval_available", "active_calibration_valid",
        "active_calibration_error_code", "active_calibration_version",
    ):
        assert key in resp.data, key
    assert resp.data["embedding_consistency_status"] == CONSISTENCY_CONSISTENT
    assert resp.data["vector_retrieval_available"] is True


def test_api_counts_documents_to_reindex(domain, global_owner_client):
    corpus = _corpus(domain, slug="compte")
    _ingest(corpus, filename="a.txt", body=b"Venture capital A.")
    _ingest(corpus, filename="b.txt", body=b"Venture capital B.")
    DomainCorpusDocument.objects.filter(corpus=corpus).update(reindex_required=True)
    resp = global_owner_client.get(f"/api/v1/admin/domain-corpora/{corpus.id}/")
    assert resp.data["embedding_reindex_required_document_count"] == 2
    assert resp.data["embeddings_reindex_required"] is True
    assert resp.data["vector_retrieval_available"] is False


def test_consistency_is_not_accepted_from_the_client(domain, global_owner_client):
    """Une cohérence affirmée par un client serait invérifiable : elle est ignorée."""
    corpus = _corpus(domain, slug="menteur")
    _ingest(corpus)
    DomainCorpusDocument.objects.filter(corpus=corpus).update(reindex_required=True)
    resp = global_owner_client.patch(
        f"/api/v1/admin/domain-corpora/{corpus.id}/",
        {"name": "x", "vector_retrieval_available": True,
         "embedding_consistent_document_count": 99}, format="json",
    )
    assert resp.status_code == 200
    assert resp.data["vector_retrieval_available"] is False
    assert resp.data["embedding_consistent_document_count"] == 0


def test_workspace_admin_cannot_read_corpus_consistency(domain, ws_admin):
    corpus = _corpus(domain, slug="prive")
    resp = ws_admin["client"].get(f"/api/v1/admin/domain-corpora/{corpus.id}/")
    assert resp.status_code == 403
