"""Éligibilité documentaire au retrieval — matrice D1..D8 (Lot 2 P0-A).

Verrouille l'invariant sur **tous** les chemins qui remontent des chunks. Les
tests portent sur le SQL réel (base de test), pas sur des doubles : le défaut
corrigé était précisément une clause absente du SQL.

Matrice de la spécification :

===  =========================  =========  =======  ==========================
Doc  Statut                     RagSpace   Chunks   Attendu au retrieval
===  =========================  =========  =======  ==========================
D1   READY                      actif      oui      **retrouvable**
D2   PENDING                    actif      oui      non retrouvable
D3   INGESTING                  actif      oui      non retrouvable
D4   FAILED                     actif      oui      non retrouvable
D5   READY                      supprimé   oui      non retrouvable
D6   READY (extraction partielle) actif    oui      retrouvable (doctrine)
D7   READY                      autre esp. oui      non retrouvable
D8   READY                      autre ws.  oui      non retrouvable
===  =========================  =========  =======  ==========================
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.rag_core.domain.eligibility import (
    ELIGIBLE_DOCUMENT_STATUS,
    eligible_chunks_queryset,
    eligible_documents_queryset,
    is_document_eligible,
)
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.repositories.chunk_repository import (
    search_chunks_hybrid,
    search_chunks_vector,
)
from apps.rag_core.repositories.document_repository import (
    list_documents_mentioning_term,
)

pytestmark = pytest.mark.django_db

TERM = "helium"
TEXT = f"Le gaz {TERM} est un composant analysé dans ce rapport trimestriel."
DIM = 768


def _mk_doc(space, status, *, filename="doc.txt", meta=None):
    return Document.objects.create(
        rag_space=space, filename=filename, status=status, meta=meta or {}
    )


def _mk_chunk(doc, *, with_embedding=True):
    return Chunk.objects.create(
        rag_space=doc.rag_space,
        document=doc,
        text=TEXT,
        chunk_index=0,
        embedding=[0.01] * DIM if with_embedding else None,
    )


@pytest.fixture
def space(workspace):
    return RagSpace.objects.create(name="espace-eligibilite", workspace=workspace)


# --------------------------------------------------------------------------- #
# D1..D4 — statut documentaire
# --------------------------------------------------------------------------- #

def test_d1_ready_document_is_retrievable(space):
    doc = _mk_doc(space, "READY")
    _mk_chunk(doc)

    fts = search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0)
    assert {r["source"]["document_id"] for r in fts} == {str(doc.id)}

    vec = search_chunks_vector(
        rag_space_id=str(space.id), query_embedding=[0.01] * DIM, top_k=10
    )
    assert {r["source"]["document_id"] for r in vec} == {str(doc.id)}


@pytest.mark.parametrize("status", ["PENDING", "INGESTING", "FAILED"])
def test_d2_d3_d4_non_ready_documents_are_never_retrievable(space, status):
    """Le cœur du NO-GO P0-A : ces chunks atteignaient le prompt et les citations."""
    doc = _mk_doc(space, status)
    _mk_chunk(doc)

    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0) == []
    assert search_chunks_vector(
        rag_space_id=str(space.id), query_embedding=[0.01] * DIM, top_k=10
    ) == []
    assert list_documents_mentioning_term(space_id=str(space.id), term=TERM) == []


# --------------------------------------------------------------------------- #
# D5 — RagSpace soft-deleted
# --------------------------------------------------------------------------- #

def test_d5_soft_deleted_space_is_never_retrievable(space):
    """25 847 chunks étaient dans ce cas, protégés par la seule permission HTTP."""
    doc = _mk_doc(space, "READY")
    _mk_chunk(doc)

    # Avant suppression : retrouvable.
    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0)

    space.deleted_at = timezone.now()
    space.save(update_fields=["deleted_at"])

    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0) == []
    assert search_chunks_vector(
        rag_space_id=str(space.id), query_embedding=[0.01] * DIM, top_k=10
    ) == []
    assert list_documents_mentioning_term(space_id=str(space.id), term=TERM) == []


# --------------------------------------------------------------------------- #
# D6 — extraction partielle : DOCTRINE explicite
# --------------------------------------------------------------------------- #

def test_d6_partial_extraction_remains_retrievable(space):
    """Décision assumée : `partial` reste éligible.

    Le rendre inéligible retirerait du retrieval du contenu aujourd'hui exploité
    (194 documents sur la base de développement). La nuance reste portée par
    ``meta.extraction.extraction_status`` et remonte en sous-statut dans l'API.
    Voir ``docs/03-backend/eligibilite-retrieval.md``.
    """
    doc = _mk_doc(
        space, "READY", meta={"extraction": {"extraction_status": "partial"}}
    )
    _mk_chunk(doc)

    rows = search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0)
    assert {r["source"]["document_id"] for r in rows} == {str(doc.id)}
    assert doc.meta["extraction"]["extraction_status"] == "partial"


# --------------------------------------------------------------------------- #
# D7 / D8 — isolation de périmètre (non-régression)
# --------------------------------------------------------------------------- #

def test_d7_other_space_is_not_retrievable(space, workspace):
    other = RagSpace.objects.create(name="autre-espace", workspace=workspace)
    doc_other = _mk_doc(other, "READY", filename="autre.txt")
    _mk_chunk(doc_other)

    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0) == []


def test_d8_other_workspace_is_not_retrievable(space):
    from apps.workspaces.models import Workspace

    ws2 = Workspace.objects.create(name="autre-workspace")
    other = RagSpace.objects.create(name="espace-ws2", workspace=ws2)
    doc_other = _mk_doc(other, "READY", filename="ws2.txt")
    _mk_chunk(doc_other)

    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0) == []


# --------------------------------------------------------------------------- #
# Le filtre s'applique AVANT LIMIT (le top-k ne doit pas être faussé)
# --------------------------------------------------------------------------- #

def test_eligibility_is_applied_before_limit(space):
    """Un document inéligible ne doit pas consommer de place dans la fenêtre top-k.

    Filtrer en Python après récupération renverrait ici 0 résultat au lieu de 1.
    """
    for i in range(5):
        _mk_chunk(_mk_doc(space, "FAILED", filename=f"ko-{i}.txt"))
    ok = _mk_doc(space, "READY", filename="ok.txt")
    _mk_chunk(ok)

    rows = search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=3, min_text_rank=0.0)
    assert [r["source"]["document_id"] for r in rows] == [str(ok.id)]


# --------------------------------------------------------------------------- #
# Prédicats ORM — cohérence avec le SQL
# --------------------------------------------------------------------------- #

def test_orm_predicates_agree_with_sql(space):
    ready = _mk_doc(space, "READY", filename="ok.txt")
    _mk_chunk(ready)
    failed = _mk_doc(space, "FAILED", filename="ko.txt")
    _mk_chunk(failed)

    assert list(eligible_documents_queryset().values_list("id", flat=True)) == [ready.id]
    assert eligible_chunks_queryset().count() == 1
    assert is_document_eligible(ready) is True
    assert is_document_eligible(failed) is False
    assert is_document_eligible(None) is False


def test_is_document_eligible_rejects_soft_deleted_space(space):
    doc = _mk_doc(space, "READY")
    space.deleted_at = timezone.now()
    space.save(update_fields=["deleted_at"])
    doc.refresh_from_db()
    assert is_document_eligible(doc) is False


def test_eligible_status_is_a_single_value():
    """Élargir cet ensemble doit être un acte délibéré, visible en revue."""
    assert ELIGIBLE_DOCUMENT_STATUS == "READY"


# --------------------------------------------------------------------------- #
# Réindexation / reaper — le statut et l'éligibilité ne divergent jamais
# --------------------------------------------------------------------------- #

def test_reindex_window_makes_document_temporarily_ineligible(space):
    """Option A : pendant INGESTING, les anciens chunks ne sont plus servis."""
    doc = _mk_doc(space, "READY")
    _mk_chunk(doc)
    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0)

    doc.status = "INGESTING"
    doc.save(update_fields=["status"])
    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0) == []

    doc.status = "READY"
    doc.save(update_fields=["status"])
    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0)


def _tentative_morte(doc):
    """Tentative plus ancienne que le seuil du reaper : seule une tentative réputée
    MORTE est récupérée (une tentative récente peut être vivante)."""
    from datetime import timedelta

    from apps.rag_core.services.ingest.reindex_jobs import ITEM_LEASE_SECONDS

    Document.objects.filter(id=doc.id).update(
        ingest_started_at=timezone.now() - timedelta(seconds=ITEM_LEASE_SECONDS + 60))


def test_reaper_restores_ready_when_a_published_index_exists(space):
    """``recover_stale_items`` (INGEST-1C-R2) : une réindexation interrompue d'un
    document qui possède un index publié ne rend pas cet index inerte. Le document
    revient READY, les chunks sont conservés et servis ; l'interruption reste tracée
    dans ``error_message``. Le statut exprime la disponibilité du dernier index
    publié, pas le sort de la tentative — statut et éligibilité restent alignés.
    """
    from apps.rag_core.services.ingest.reindex_jobs import recover_stale_items

    doc = _mk_doc(space, "INGESTING")
    chunk = _mk_chunk(doc)
    _tentative_morte(doc)

    recover_stale_items(dry_run=False)
    doc.refresh_from_db()

    assert doc.status == "READY"
    assert doc.error_message == "reindex_interrupted"
    assert list(Chunk.objects.filter(document=doc).values_list("id", flat=True)) == [chunk.id]
    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0)


def test_reaper_first_ingestion_without_index_stays_failed(space):
    """Contrôle : une PREMIÈRE ingestion interrompue (aucun chunk publié) reste
    FAILED — la restauration READY exige un index publié."""
    from apps.rag_core.services.ingest.reindex_jobs import recover_stale_items

    doc = _mk_doc(space, "INGESTING")
    _tentative_morte(doc)

    recover_stale_items(dry_run=False)
    doc.refresh_from_db()

    assert doc.status == "FAILED"
    assert Chunk.objects.filter(document=doc).count() == 0
    assert search_chunks_hybrid(rag_space_id=str(space.id), question=TERM, top_k=10, min_text_rank=0.0) == []
