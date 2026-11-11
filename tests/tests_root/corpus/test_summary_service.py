"""Tests du SERVICE de résumé de corpus métier (hors HTTP).

Trois zones que l'API ne couvre pas directement :
- la primitive de récupération des chunks (ordre, scope page, exclusions) ;
- l'exécution asynchrone et sa **revérification d'accès** ;
- l'observabilité (événements structurés) et son allowlist.
"""
import pytest

import apps.corpus.services.summary as svc
from apps.corpus.models import (
    CorpusDocumentStatus,
    DomainCorpusChunk,
    DomainCorpusDocument,
    DomainCorpusDocumentSummary,
)
from apps.corpus.services import summary_events as events
from apps.corpus.services.access import load_accessible_domain_corpus_document
from apps.rag_core.models import RagSpace
from apps.rag_core.services.summary_service import DocumentHasNoContent
from apps.workspaces.models import Membership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _enable_feature(settings):
    settings.DOMAIN_CORPUS_SUMMARY_ENABLED = True
    return settings


@pytest.fixture
def doc(domain_corpus):
    d = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="ref.pdf", status=CorpusDocumentStatus.READY
    )
    # Insérés dans le DÉSORDRE : l'ordre de restitution doit venir de
    # `chunk_index`, jamais de l'ordre d'insertion.
    for idx, page, text in [
        (2, 5, "Troisième."),
        (0, 5, "Premier."),
        (1, 5, "Deuxième."),
        (3, 6, "Autre page."),
    ]:
        DomainCorpusChunk.objects.create(
            corpus=domain_corpus, document=d, chunk_index=idx, text=text, page_number=page
        )
    return d


@pytest.fixture
def consumer(db, user, workspace, rag_space, domain_corpus, grant_access):
    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Membership.ROLE_OWNER}
    )
    grant_access(workspace=workspace, corpus=domain_corpus)
    return {"user": user, "workspace": workspace, "rag_space": rag_space, "corpus": domain_corpus}


# ---------------------------------------------------------------------------
# Récupération des chunks
# ---------------------------------------------------------------------------
def test_chunks_are_ordered_by_chunk_index(doc):
    rows = svc.fetch_domain_corpus_summary_chunks(doc, scope="document")
    assert [c.text for c in rows] == ["Premier.", "Deuxième.", "Troisième.", "Autre page."]


def test_page_scope_returns_only_that_page(doc):
    rows = svc.fetch_domain_corpus_summary_chunks(doc, scope="page", page=6)
    assert [c.text for c in rows] == ["Autre page."]


def test_page_scope_never_falls_back_to_other_pages(doc):
    with pytest.raises(DocumentHasNoContent):
        svc.fetch_domain_corpus_summary_chunks(doc, scope="page", page=99)


def test_blank_chunks_are_excluded(domain_corpus):
    d = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="mixte.pdf", status=CorpusDocumentStatus.READY
    )
    DomainCorpusChunk.objects.create(
        corpus=domain_corpus, document=d, chunk_index=0, text="  \n ", page_number=1
    )
    DomainCorpusChunk.objects.create(
        corpus=domain_corpus, document=d, chunk_index=1, text="Utile.", page_number=1
    )

    rows = svc.fetch_domain_corpus_summary_chunks(d, scope="document")
    assert [c.text for c in rows] == ["Utile."]


def test_chunks_of_other_documents_are_never_included(domain_corpus, doc):
    other = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="voisin.pdf", status=CorpusDocumentStatus.READY
    )
    DomainCorpusChunk.objects.create(
        corpus=domain_corpus, document=other, chunk_index=0, text="NE DOIT PAS APPARAÎTRE"
    )

    rows = svc.fetch_domain_corpus_summary_chunks(doc, scope="document")
    assert all("NE DOIT PAS" not in c.text for c in rows)


# ---------------------------------------------------------------------------
# Résolveur d'accès
# ---------------------------------------------------------------------------
def test_resolver_returns_document_when_authorized(consumer, doc):
    got = load_accessible_domain_corpus_document(
        user=consumer["user"], workspace=consumer["workspace"], document_id=doc.id
    )
    assert got.id == doc.id


def test_resolver_refuses_without_workspace(doc):
    from rest_framework.exceptions import NotFound

    with pytest.raises(NotFound):
        load_accessible_domain_corpus_document(user=None, workspace=None, document_id=doc.id)


def test_resolver_returns_document_even_if_not_ready(consumer, doc):
    """Le STATUT n'est pas un contrôle d'accès : « pas prêt » ≠ « inexistant »."""
    doc.status = CorpusDocumentStatus.PENDING
    doc.save(update_fields=["status"])

    got = load_accessible_domain_corpus_document(
        user=consumer["user"], workspace=consumer["workspace"], document_id=doc.id
    )
    assert got.status == CorpusDocumentStatus.PENDING


# ---------------------------------------------------------------------------
# Politique LLM : espace consommateur obligatoire
# ---------------------------------------------------------------------------
def test_missing_consumer_space_is_refused(consumer, doc):
    with pytest.raises(svc.MissingConsumerSpace):
        svc.summarize_corpus_document(
            user=consumer["user"], workspace=consumer["workspace"],
            rag_space=None, document=doc,
        )


# ---------------------------------------------------------------------------
# Asynchrone : revérification d'accès à l'exécution
# ---------------------------------------------------------------------------
def _pending_row(consumer, doc):
    return DomainCorpusDocumentSummary.objects.create(
        document=doc, corpus=consumer["corpus"], workspace=consumer["workspace"],
        rag_space=consumer["rag_space"], variant_key="bullets:document::fr",
        mode="bullets", scope="document", language="fr", status="PENDING",
    )


def test_async_run_revalidates_access(consumer, doc, grant_access, monkeypatch):
    """L'habilitation retirée APRÈS l'enqueue doit bloquer la génération."""
    row = _pending_row(consumer, doc)
    grant_access(workspace=consumer["workspace"], corpus=consumer["corpus"], enabled=False)

    called = []
    monkeypatch.setattr(svc, "generate_text", lambda **kw: called.append(kw) or {"text": "x"})

    with pytest.raises(svc.CorpusAccessRevoked):
        svc.run_corpus_summary_generation_for_row(row)

    # Aucune génération : le refus intervient AVANT tout appel LLM, donc aucun
    # contenu de corpus ne part vers un fournisseur.
    assert called == []


def test_async_run_generates_when_access_still_valid(consumer, doc, monkeypatch):
    row = _pending_row(consumer, doc)
    monkeypatch.setattr(
        svc, "generate_text",
        lambda **kw: {"text": "SYNTHÈSE", "provider": "ollama", "model": "m", "tokens": 3},
    )

    res = svc.run_corpus_summary_generation_for_row(row, user_id=consumer["user"].id)

    assert res.status == "ready"
    row.refresh_from_db()
    assert row.status == "READY"
    assert row.summary == "SYNTHÈSE"


def test_mark_row_failed_sets_stable_code(consumer, doc):
    row = _pending_row(consumer, doc)
    svc.mark_row_failed(row, code="enqueue_failed", message="broker down")

    row.refresh_from_db()
    assert row.status == "FAILED"
    assert row.error_code == "enqueue_failed"


def test_reaper_marks_stale_pending_rows(consumer, doc):
    from datetime import timedelta

    from django.utils import timezone

    row = _pending_row(consumer, doc)
    DomainCorpusDocumentSummary.objects.filter(id=row.id).update(
        updated_at=timezone.now() - timedelta(seconds=5000)
    )

    assert svc.reap_stale_corpus_summaries(timeout_s=900) == 1
    row.refresh_from_db()
    assert row.status == "FAILED"
    assert row.error_code == "stale_timeout"


# ---------------------------------------------------------------------------
# Le reaper dans sa forme d'exécution RÉELLE : sans délai explicite
#
# Le test ci-dessus passe `timeout_s=900`, ce qui court-circuite
# `_pending_timeout_s()` — la seule ligne qui a échoué en production pendant
# 37 jours (`setting_int()` appelé avec un défaut, contrat à un seul argument).
# Les tests suivants empruntent le chemin de l'ordonnanceur.
# ---------------------------------------------------------------------------
def test_corpus_reaper_without_explicit_timeout_uses_the_registry(consumer, doc):
    from datetime import timedelta

    from django.utils import timezone

    row = _pending_row(consumer, doc)
    DomainCorpusDocumentSummary.objects.filter(id=row.id).update(
        updated_at=timezone.now() - timedelta(seconds=5000)
    )

    assert svc.reap_stale_corpus_summaries() == 1  # <- SANS timeout_s

    row.refresh_from_db()
    assert row.status == "FAILED"
    assert row.error_code == "stale_timeout"


def test_corpus_reaper_without_timeout_leaves_fresh_rows_alone(consumer, doc):
    """Un PENDING récent n'est pas un PENDING bloqué."""
    row = _pending_row(consumer, doc)

    assert svc.reap_stale_corpus_summaries() == 0

    row.refresh_from_db()
    assert row.status == "PENDING"


def test_corpus_reaper_task_runs_with_no_argument(consumer, doc):
    """La forme sous laquelle l'ordonnanceur l'appelle réellement."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.corpus.tasks import reap_stale_domain_corpus_summaries_task

    row = _pending_row(consumer, doc)
    DomainCorpusDocumentSummary.objects.filter(id=row.id).update(
        updated_at=timezone.now() - timedelta(seconds=5000)
    )

    result = reap_stale_domain_corpus_summaries_task()  # <- aucun argument

    assert isinstance(result, dict)
    row.refresh_from_db()
    assert row.status == "FAILED"


def test_corpus_pending_timeout_comes_from_the_registry():
    """Le défaut appartient au registre, pas au service."""
    assert svc._pending_timeout_s() == 900


# ---------------------------------------------------------------------------
# Observabilité
# ---------------------------------------------------------------------------
def test_events_drop_fields_outside_the_allowlist():
    payload = events.emit(
        events.EVENT_COMPLETED,
        document_id="d-1",
        # Champs interdits : ils ne doivent JAMAIS ressortir, même passés par erreur.
        prompt="Tu résumes UNIQUEMENT…",
        text="Page treize : les fonds d'amorçage.",
        summary_text="Le rapport indique…",
        api_key="sk-secret",
        email="user@example.com",
    )

    assert payload["document_id"] == "d-1"
    for forbidden in ("prompt", "text", "summary_text", "api_key", "email"):
        assert forbidden not in payload


def test_events_drop_structured_values():
    """Une structure (dict/list) est écartée : c'est par là qu'un payload entrerait."""
    payload = events.emit(events.EVENT_REQUESTED, reason={"nested": "chunk text"})
    assert "reason" not in payload


def test_service_emits_lifecycle_events(consumer, doc, monkeypatch, caplog):
    monkeypatch.setattr(
        svc, "generate_text",
        lambda **kw: {"text": "OK", "provider": "ollama", "model": "m", "tokens": 1},
    )
    seen = []
    monkeypatch.setattr(events, "emit", lambda name, **kw: seen.append(name) or {})

    svc.summarize_corpus_document(
        user=consumer["user"], workspace=consumer["workspace"],
        rag_space=consumer["rag_space"], document=doc,
    )

    assert events.EVENT_REQUESTED in seen
    assert events.EVENT_STARTED in seen
    assert events.EVENT_COMPLETED in seen


def test_denied_access_emits_a_reason(consumer, doc, grant_access, monkeypatch):
    grant_access(workspace=consumer["workspace"], corpus=consumer["corpus"], enabled=False)
    seen = []
    monkeypatch.setattr(events, "emit", lambda name, **kw: seen.append((name, kw)) or {})

    with pytest.raises(svc.CorpusAccessRevoked):
        svc.summarize_corpus_document(
            user=consumer["user"], workspace=consumer["workspace"],
            rag_space=consumer["rag_space"], document=doc,
        )

    names = [n for n, _ in seen]
    assert events.EVENT_ACCESS_DENIED in names
    reasons = [kw.get("reason") for n, kw in seen if n == events.EVENT_ACCESS_DENIED]
    assert "corpus_not_authorized" in reasons


def test_feature_disabled_emits_distinct_reason(consumer, doc, settings, monkeypatch):
    """Le 404 est unique côté HTTP ; la cause interne, elle, reste différenciable."""
    settings.DOMAIN_CORPUS_SUMMARY_ENABLED = False
    seen = []
    monkeypatch.setattr(events, "emit", lambda name, **kw: seen.append((name, kw)) or {})

    with pytest.raises(svc.CorpusSummaryDisabled) as exc:
        svc.summarize_corpus_document(
            user=consumer["user"], workspace=consumer["workspace"],
            rag_space=consumer["rag_space"], document=doc,
        )

    # Code PUBLIC indistinguable d'un document inexistant…
    assert exc.value.code == "not_found"
    # …mais cause interne explicite dans le journal.
    assert "feature_disabled" in [kw.get("reason") for _, kw in seen]
