"""Réindexation durable d'un RagSpace — jobs asynchrones, reprise, idempotence.

Couvre le contrat 202, les permissions, la concurrence (un seul job actif),
l'idempotence des items, l'isolation des échecs, la finalisation, la reprise des
items « stale » et la relance des échecs. L'extraction réelle est simulée
(``reindex_existing_document`` monkeypatché) : on teste ici la machine à états du
job, pas le pipeline d'extraction (déjà couvert ailleurs).
"""
from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.rag_core.models import RagSpace
from apps.rag_core.models.document import Document, DocumentStatus
from apps.rag_core.models.reindex_job import (
    RagSpaceReindexJob,
    RagSpaceReindexJobItem,
    ReindexItemStatus,
    ReindexJobStatus,
)
from apps.workspaces.models import Membership

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# Fixtures locales (owner/admin requis par l'endpoint)
# --------------------------------------------------------------------------- #
@pytest.fixture
def owner_client(db, user, workspace):
    from rest_framework.test import APIClient

    Membership.objects.create(workspace=workspace, user=user, role=Membership.ROLE_OWNER)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def space(db, workspace):
    return RagSpace.objects.create(name="reindex-space", workspace=workspace)


def _doc(space, name="a.xlsx", status=DocumentStatus.READY, with_file=True):
    d = Document.objects.create(
        rag_space=space, filename=name, content_type="application/vnd.openxmlformats",
        size_bytes=10, status=status,
    )
    if with_file:
        d.storage_file.name = f"rag_docs/{space.id}/{d.id}/{name}"
        d.save(update_fields=["storage_file"])
    return d


def _run_job_inline(job):
    """Simule ce que fait le worker : job → RUNNING puis chaque item traité en direct.

    On appelle les tâches document DIRECTEMENT (pas via `.delay`) pour un test
    déterministe, indépendant de la configuration Celery/broker. Le fan-out réel de
    l'orchestrateur est validé dynamiquement avec un worker (cf. rapport lot 2).
    """
    from apps.rag_core.tasks import reindex_job_item_task

    RagSpaceReindexJob.objects.filter(id=job.id).update(
        status=ReindexJobStatus.RUNNING, started_at=timezone.now()
    )
    for item_id in RagSpaceReindexJobItem.objects.filter(job=job).values_list("id", flat=True):
        reindex_job_item_task(item_id=str(item_id))


# --------------------------------------------------------------------------- #
# 25.1 — Lancement : 202, job + items, AUCUNE extraction dans la requête HTTP
# --------------------------------------------------------------------------- #
def test_post_returns_202_and_creates_job_without_extraction(owner_client, space, monkeypatch):
    _doc(space, "a.xlsx")
    _doc(space, "b.xlsx")

    called = {"enqueue": 0, "extract": 0}
    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator",
        lambda job_id: called.__setitem__("enqueue", called["enqueue"] + 1),
    )
    monkeypatch.setattr(
        "apps.rag_core.services.ingest_document.reindex_existing_document",
        lambda **k: called.__setitem__("extract", called["extract"] + 1),
    )

    resp = owner_client.post(f"/api/v1/rag-spaces/{space.id}/reindex/")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["total_documents"] == 2
    assert body["already_running"] is False
    job = RagSpaceReindexJob.objects.get(id=body["job_id"])
    assert job.status == ReindexJobStatus.PENDING
    assert RagSpaceReindexJobItem.objects.filter(job=job).count() == 2
    # AUCUNE extraction n'a eu lieu dans la requête HTTP.
    assert called["extract"] == 0


def test_post_skips_documents_without_storage_file(owner_client, space, monkeypatch):
    _doc(space, "ok.xlsx", with_file=True)
    _doc(space, "nofile.xlsx", with_file=False)
    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )
    resp = owner_client.post(f"/api/v1/rag-spaces/{space.id}/reindex/")
    assert resp.status_code == 202
    assert resp.json()["total_documents"] == 1


# --------------------------------------------------------------------------- #
# 25.2 — Permissions
# --------------------------------------------------------------------------- #
def test_viewer_and_member_denied(db, space, workspace, monkeypatch):
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )
    User = get_user_model()
    for role in (Membership.ROLE_VIEWER, Membership.ROLE_MEMBER):
        u = User.objects.create_user(username=f"u-{role}", password="pw")
        Membership.objects.create(workspace=workspace, user=u, role=role)
        c = APIClient()
        c.force_authenticate(user=u)
        resp = c.post(f"/api/v1/rag-spaces/{space.id}/reindex/")
        assert resp.status_code == 403, role


def test_outsider_denied(db, space):
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    other = get_user_model().objects.create_user(username="out", password="pw")
    c = APIClient()
    c.force_authenticate(user=other)
    resp = c.post(f"/api/v1/rag-spaces/{space.id}/reindex/")
    assert resp.status_code in (403, 404)


# --------------------------------------------------------------------------- #
# 25.3 — Concurrence : un seul job actif par espace → 409 + job existant
# --------------------------------------------------------------------------- #
def test_concurrent_job_returns_existing(owner_client, space, monkeypatch, user):
    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )
    existing = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.RUNNING, total_count=1
    )
    resp = owner_client.post(f"/api/v1/rag-spaces/{space.id}/reindex/")
    assert resp.status_code == 409
    body = resp.json()
    assert body["already_running"] is True
    assert body["job_id"] == str(existing.id)


def test_active_unique_constraint_blocks_second(space, user):
    from django.db import IntegrityError

    RagSpaceReindexJob.objects.create(rag_space=space, requested_by=user, status=ReindexJobStatus.RUNNING)
    with pytest.raises(IntegrityError):
        RagSpaceReindexJob.objects.create(rag_space=space, requested_by=user, status=ReindexJobStatus.PENDING)


# --------------------------------------------------------------------------- #
# 25.4 — Item idempotent : double livraison → un seul remplacement, compteurs OK
# --------------------------------------------------------------------------- #
def test_item_idempotent(space, user, monkeypatch):
    from apps.rag_core.tasks import reindex_job_item_task

    calls = {"n": 0}
    monkeypatch.setattr(
        "apps.rag_core.services.ingest_document.reindex_existing_document",
        lambda **k: calls.__setitem__("n", calls["n"] + 1),
    )
    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.RUNNING,
        total_count=1, started_at=timezone.now(),
    )
    item = RagSpaceReindexJobItem.objects.create(
        job=job, document=_doc(space), status=ReindexItemStatus.QUEUED
    )

    reindex_job_item_task(item_id=str(item.id))
    reindex_job_item_task(item_id=str(item.id))  # double livraison

    item.refresh_from_db()
    assert item.status == ReindexItemStatus.SUCCEEDED
    assert item.attempt_count == 1           # pas incrémenté deux fois
    assert calls["n"] == 1                   # une seule ré-extraction


# --------------------------------------------------------------------------- #
# 25.5 — Échec d'enqueue : job FAILED, pas de faux succès
# --------------------------------------------------------------------------- #
def test_enqueue_failure_marks_job_failed(space, user, monkeypatch):
    from apps.rag_core.services.ingest.reindex_jobs import _enqueue_orchestrator

    def _boom(**k):
        raise RuntimeError("broker down")

    monkeypatch.setattr("apps.rag_core.tasks.run_reindex_job_task.delay", _boom)
    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.PENDING, total_count=1
    )
    _enqueue_orchestrator(str(job.id))
    job.refresh_from_db()
    assert job.status == ReindexJobStatus.FAILED
    assert job.error_code == "reindex_enqueue_unavailable"


# --------------------------------------------------------------------------- #
# 25.6 / 25.7 — Échec isolé + succès ; finalisation COMPLETED_WITH_ERRORS
# --------------------------------------------------------------------------- #
def test_document_failure_isolated_and_finalize(space, user, monkeypatch):
    from apps.rag_core.services.ingest.types import IngestError

    d_ok, d_bad = _doc(space, "ok.xlsx"), _doc(space, "bad.xlsx")

    def _fake(**kwargs):
        if str(kwargs.get("document_id")) == str(d_bad.id):
            raise IngestError("boom")
        return None

    monkeypatch.setattr(
        "apps.rag_core.services.ingest_document.reindex_existing_document", _fake
    )
    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.PENDING, total_count=2
    )
    for d in (d_ok, d_bad):
        RagSpaceReindexJobItem.objects.create(job=job, document=d, status=ReindexItemStatus.PENDING)

    _run_job_inline(job)

    job.refresh_from_db()
    assert job.status == ReindexJobStatus.COMPLETED_WITH_ERRORS
    assert job.succeeded_count == 1 and job.failed_count == 1
    assert job.progress_percent == 100.0
    statuses = set(RagSpaceReindexJobItem.objects.filter(job=job).values_list("status", flat=True))
    assert statuses == {ReindexItemStatus.SUCCEEDED, ReindexItemStatus.FAILED}


def test_item_failed_when_document_result_failed(space, user, monkeypatch):
    """Un IngestResult FAILED (classeur trop gros, timeout…) RENVOYÉ sans exception
    doit marquer l'item FAILED — pas SUCCEEDED (sinon le job masque des échecs)."""
    from apps.rag_core.models.document import DocumentStatus
    from apps.rag_core.tasks import reindex_job_item_task

    class _Res:
        status = DocumentStatus.FAILED

    monkeypatch.setattr(
        "apps.rag_core.services.ingest_document.reindex_existing_document",
        lambda **k: _Res(),
    )
    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.RUNNING,
        total_count=1, started_at=timezone.now(),
    )
    item = RagSpaceReindexJobItem.objects.create(
        job=job, document=_doc(space), status=ReindexItemStatus.QUEUED
    )
    reindex_job_item_task(item_id=str(item.id))
    item.refresh_from_db()
    assert item.status == ReindexItemStatus.FAILED
    job.refresh_from_db()
    assert job.status == ReindexJobStatus.COMPLETED_WITH_ERRORS
    assert job.failed_count == 1


def test_all_success_finalizes_completed(space, user, monkeypatch):
    monkeypatch.setattr(
        "apps.rag_core.services.ingest_document.reindex_existing_document", lambda **k: None
    )
    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.PENDING, total_count=2
    )
    for i in range(2):
        RagSpaceReindexJobItem.objects.create(job=job, document=_doc(space, f"d{i}.xlsx"),
                                              status=ReindexItemStatus.PENDING)
    _run_job_inline(job)
    job.refresh_from_db()
    assert job.status == ReindexJobStatus.COMPLETED
    assert job.succeeded_count == 2


# --------------------------------------------------------------------------- #
# 26 — Reprise : item RUNNING lease expiré → INTERRUPTED, sans duplication
# --------------------------------------------------------------------------- #
def test_recover_stale_running_item(space, user):
    from datetime import timedelta

    from apps.rag_core.services.ingest.reindex_jobs import recover_stale_items

    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.RUNNING,
        total_count=1, started_at=timezone.now(),
    )
    item = RagSpaceReindexJobItem.objects.create(
        job=job, document=_doc(space), status=ReindexItemStatus.RUNNING, attempt_count=1,
        started_at=timezone.now() - timedelta(hours=2),
        lease_expires_at=timezone.now() - timedelta(hours=1),   # lease expiré
    )
    dry = recover_stale_items(dry_run=True)
    assert str(item.id) in dry["stale_items"]

    recover_stale_items(dry_run=False)
    item.refresh_from_db()
    assert item.status == ReindexItemStatus.INTERRUPTED
    assert item.attempt_count == 1                 # historique conservé
    job.refresh_from_db()
    assert job.status == ReindexJobStatus.INTERRUPTED


def _tentative_morte(doc):
    """Tentative plus ancienne que le seuil du reaper (réputée morte)."""
    from datetime import timedelta

    from apps.rag_core.services.ingest.reindex_jobs import ITEM_LEASE_SECONDS

    Document.objects.filter(id=doc.id).update(
        ingest_started_at=timezone.now() - timedelta(seconds=ITEM_LEASE_SECONDS + 60))


def test_recover_stale_ingesting_document(space):
    """Première ingestion interrompue (aucun chunk publié) : FAILED, comme avant."""
    from apps.rag_core.services.ingest.reindex_jobs import recover_stale_items

    d = _doc(space, "stuck.xlsx", status=DocumentStatus.INGESTING)
    _tentative_morte(d)
    recover_stale_items(dry_run=False)
    d.refresh_from_db()
    assert d.status == DocumentStatus.FAILED
    assert d.error_message == "reindex_interrupted"


def test_recover_stale_ingesting_document_with_published_index_is_ready(space):
    """INGEST-1C-R2 : réindexation interrompue d'un document déjà publié — l'ancien
    index reste actif (READY), la tentative interrompue est tracée dans error_message."""
    from apps.rag_core.models import Chunk
    from apps.rag_core.services.ingest.reindex_jobs import recover_stale_items

    d = _doc(space, "served.xlsx", status=DocumentStatus.INGESTING)
    chunk = Chunk.objects.create(rag_space=space, document=d, chunk_index=0, text="ancien index")
    _tentative_morte(d)
    recover_stale_items(dry_run=False)
    d.refresh_from_db()
    assert d.status == DocumentStatus.READY
    assert d.error_message == "reindex_interrupted"
    assert list(Chunk.objects.filter(document=d).values_list("id", flat=True)) == [chunk.id]


# --------------------------------------------------------------------------- #
# Relance des échecs — nouveau job lié, historique conservé
# --------------------------------------------------------------------------- #
def test_retry_failed_creates_child_job(owner_client, space, user, monkeypatch):
    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )
    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.COMPLETED_WITH_ERRORS,
        total_count=2, succeeded_count=1, failed_count=1, finished_at=timezone.now(),
    )
    RagSpaceReindexJobItem.objects.create(job=job, document=_doc(space, "ok.xlsx"),
                                          status=ReindexItemStatus.SUCCEEDED)
    RagSpaceReindexJobItem.objects.create(job=job, document=_doc(space, "bad.xlsx"),
                                          status=ReindexItemStatus.FAILED)

    resp = owner_client.post(f"/api/v1/reindex-jobs/{job.id}/retry-failed/")
    assert resp.status_code == 202
    new_id = resp.json()["job_id"]
    new_job = RagSpaceReindexJob.objects.get(id=new_id)
    assert new_job.parent_job_id == job.id
    assert new_job.total_count == 1
    assert RagSpaceReindexJobItem.objects.filter(job=new_job).count() == 1
    # L'historique du job d'origine est intact.
    assert RagSpaceReindexJob.objects.filter(id=job.id).exists()


# --------------------------------------------------------------------------- #
# Consultation + job actif (reprise après refresh)
# --------------------------------------------------------------------------- #
def test_get_job_status_and_active_job(owner_client, space, user):
    job = RagSpaceReindexJob.objects.create(
        rag_space=space, requested_by=user, status=ReindexJobStatus.RUNNING,
        total_count=3, succeeded_count=1, running_count=2, started_at=timezone.now(),
    )
    r = owner_client.get(f"/api/v1/reindex-jobs/{job.id}/")
    assert r.status_code == 200
    assert r.json()["progress_percent"] == pytest.approx(33.33, abs=0.01)

    r2 = owner_client.get(f"/api/v1/rag-spaces/{space.id}/active-reindex-job/")
    assert r2.status_code == 200
    assert r2.json()["active"] is True
    assert r2.json()["job"]["job_id"] == str(job.id)


def test_get_unknown_job_404(owner_client):
    r = owner_client.get(f"/api/v1/reindex-jobs/{uuid.uuid4()}/")
    assert r.status_code == 404
