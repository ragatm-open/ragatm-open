"""REINDEX-GUARD-1 — admission d'une réindexation EXPLICITE (POST /documents/<id>/reindex/).

Politique (REINDEX-GUARD-0) :

    READY      -> autorisé
    FAILED     -> règles de retryabilité existantes (inchangées)
    PENDING    -> 409 ``reindex_already_in_progress`` sauf ``force=true``
    INGESTING  -> 409 ``reindex_already_in_progress`` sauf ``force=true``

L'admission est ATOMIQUE (verrou de la ligne document, transition de statut AVANT
l'enqueue ou l'exécution inline) : deux POST simultanés donnent exactement un 200 et
un 409, une seule opération. Les chemins internes (retry worker, items de job, reaper,
redélivrance) ne passent pas par cette garde. Aucun provider réel : enqueue capté,
pipeline doublé.
"""
from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from api.v1.documents import views_common, views_write
from apps.audit.models import ActionAuditEvent
from apps.rag_core.models import Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.models.reindex_job import (
    RagSpaceReindexJob,
    RagSpaceReindexJobItem,
    ReindexItemStatus,
    ReindexJobStatus,
)
from apps.rag_core.services.ingest.types import IngestResult
from django.core.files.base import ContentFile
from django.db import connection
from django.urls import reverse
from django.utils import timezone

pytestmark = pytest.mark.django_db

CONFLICT = "reindex_already_in_progress"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")


def _doc(rag_space, status, *, error_message=""):
    doc = Document.objects.create(
        rag_space=rag_space, filename="doc.txt", content_type="text/plain", size_bytes=1,
        sha256=uuid.uuid4().hex, status=status, error_message=error_message,
    )
    doc.storage_file.save("doc.txt", ContentFile(b"Contrat de reindex. " * 20), save=True)
    return doc


def _url(doc) -> str:
    return reverse("documents-reindex", args=[str(doc.id)])


def _capture_async(monkeypatch, settings) -> list:
    """Chemin ASYNC : Celery non eager, enqueue capté (aucun broker)."""
    settings.CELERY_TASK_ALWAYS_EAGER = False
    monkeypatch.setattr(views_write, "is_celery_eager_mode", lambda: False)
    calls: list = []

    def _fake_enqueue(*, request, doc, ocr=None, extraction_timeout_seconds=None):
        calls.append({"document_id": str(doc.id), "status_at_enqueue": Document.objects.get(id=doc.id).status})
        return None

    monkeypatch.setattr(views_write, "enqueue_async_or_503", _fake_enqueue)
    return calls


def _capture_sync(monkeypatch, settings, *, gate: threading.Event | None = None) -> list:
    """Chemin SYNC (eager) : exécution inline doublée ; ``gate`` retient l'exécution."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    calls: list = []

    def _fake_reindex(*, document_id, **kwargs):
        calls.append({"document_id": document_id, "status_at_start": Document.objects.get(id=document_id).status})
        if gate is not None:
            if len(calls) == 1:
                assert gate.wait(timeout=15), "porte de synchronisation non levée"
            else:
                gate.set()  # une seconde exécution (force) libère la première
        Document.objects.filter(id=document_id).update(status=DocumentStatus.READY, error_message="")
        return IngestResult(document_id=document_id, status=DocumentStatus.READY, chunks_created=1, deduped=False)

    monkeypatch.setattr(views_write, "reindex_existing_document", _fake_reindex)
    return calls


def _reindex_events(doc):
    return list(ActionAuditEvent.objects.filter(action_name="document.reindex", resource_id=str(doc.id))
                .order_by("id").values_list("event_name", "metadata"))


# --------------------------------------------------------------------------- #
# G1 / G2 — READY et FAILED : comportement existant conservé
# --------------------------------------------------------------------------- #
def test_g1_ready_explicit_reindex_accepted_async(api_client, rag_space, settings, monkeypatch):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.READY)

    resp = api_client.post(_url(doc) + "?async=true", data={"strategy": "standard"}, format="json")

    assert resp.status_code == 200 and resp.json()["queued"] is True
    assert len(calls) == 1


def test_g1_ready_explicit_reindex_accepted_sync(api_client, rag_space, settings, monkeypatch):
    calls = _capture_sync(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.READY)

    resp = api_client.post(_url(doc), data={"strategy": "standard"}, format="json")

    assert resp.status_code == 200 and resp.json()["ok"] is True and resp.json()["queued"] is False
    assert len(calls) == 1


def test_g2_failed_retryable_accepted(api_client, rag_space, settings, monkeypatch):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.FAILED, error_message="transient_ingest_error: EmbeddingsError")

    resp = api_client.post(_url(doc) + "?async=true", data={}, format="json")

    assert resp.status_code == 200 and len(calls) == 1


@pytest.mark.parametrize("force", [False, True])
def test_g2_failed_non_retryable_stays_400_even_with_force(api_client, rag_space, settings, monkeypatch, force):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.FAILED, error_message="executable_refused: fichier exécutable")

    resp = api_client.post(_url(doc) + "?async=true", data={"force": force}, format="json")

    assert resp.status_code == 400 and resp.json()["detail"] == "document_not_eligible"
    assert calls == [] and Document.objects.get(id=doc.id).status == DocumentStatus.FAILED


@pytest.mark.parametrize("bad", ["true", 1, [], "yes"])
def test_force_must_be_a_json_boolean(api_client, rag_space, settings, monkeypatch, bad):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.READY)

    resp = api_client.post(_url(doc) + "?async=true", data={"force": bad}, format="json")

    assert resp.status_code == 400 and resp.json()["detail"] == "invalid_force"
    assert calls == []


# --------------------------------------------------------------------------- #
# G3 / G4 — PENDING / INGESTING : 409 sauf force
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [DocumentStatus.PENDING, DocumentStatus.INGESTING])
def test_g3_g4_in_progress_without_force_is_409_and_starts_nothing(api_client, rag_space, settings, monkeypatch, status):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, status)

    resp = api_client.post(_url(doc) + "?async=true", data={"strategy": "standard"}, format="json")

    assert resp.status_code == 409, resp.content
    body = resp.json()
    assert body["ok"] is False
    assert body["detail"] == CONFLICT and body["error_code"] == CONFLICT
    assert body["document_id"] == str(doc.id) and body["status"] == status
    assert body["force_allowed"] is True
    assert "active_job_id" not in body and "active_item_id" not in body and "celery_task_id" not in body
    assert calls == [], "aucune opération ne doit démarrer"
    assert Document.objects.get(id=doc.id).status == status
    assert _reindex_events(doc) == [], "une demande refusée ne produit aucun événement de travail"


@pytest.mark.parametrize("status", [DocumentStatus.PENDING, DocumentStatus.INGESTING])
def test_g3_g4_in_progress_with_force_is_admitted_once(api_client, rag_space, settings, monkeypatch, status):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, status)

    resp = api_client.post(_url(doc) + "?async=true", data={"strategy": "standard", "force": True}, format="json")

    assert resp.status_code == 200 and resp.json()["queued"] is True
    assert len(calls) == 1 and calls[0]["status_at_enqueue"] == DocumentStatus.PENDING
    events = _reindex_events(doc)
    assert [name for name, _ in events] and all(md.get("forced") is True for _, md in events), events
    assert any(name == "action.detected" for name, _ in events), events


def test_same_or_different_strategy_both_refused_without_force(api_client, rag_space, settings, monkeypatch):
    """Sans ``force``, une stratégie différente ne justifie pas une seconde opération."""
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.INGESTING)

    for strategy in ("standard", "extended_timeout"):
        resp = api_client.post(_url(doc) + "?async=true", data={"strategy": strategy}, format="json")
        assert resp.status_code == 409, strategy
    assert calls == []


# --------------------------------------------------------------------------- #
# G5 — deux requêtes simultanées : admission atomique (PostgreSQL, threads)
# --------------------------------------------------------------------------- #
def _two_simultaneous_posts(user, doc, *, body, query="") -> list[tuple[int, dict]]:
    from rest_framework.test import APIClient

    barrier = threading.Barrier(2)
    out: list = [None, None]

    def _req(i: int) -> None:
        try:
            client = APIClient()
            client.force_authenticate(user=user)
            barrier.wait(timeout=10)
            resp = client.post(_url(doc) + query, data=body, format="json")
            out[i] = (resp.status_code, resp.json())
        except BaseException as exc:  # noqa: BLE001
            out[i] = (0, {"error": repr(exc)})
        finally:
            connection.close()

    threads = [threading.Thread(target=_req, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(t is not None and t[0] != 0 for t in out), out
    return out  # type: ignore[return-value]


@pytest.mark.django_db(transaction=True)
def test_g5_two_simultaneous_async_requests_admit_exactly_one(user, member, rag_space, settings, monkeypatch):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.READY)

    out = _two_simultaneous_posts(user, doc, body={"strategy": "standard"}, query="?async=true")

    codes = sorted(c for c, _ in out)
    assert codes == [200, 409], out
    rejected = next(b for c, b in out if c == 409)
    assert rejected["error_code"] == CONFLICT and rejected["status"] == DocumentStatus.PENDING
    assert len(calls) == 1, "exactement une opération"
    assert Document.objects.get(id=doc.id).status == DocumentStatus.PENDING


@pytest.mark.django_db(transaction=True)
def test_g5_two_simultaneous_sync_requests_admit_exactly_one(user, member, rag_space, settings, monkeypatch):
    gate = threading.Event()
    calls = _capture_sync(monkeypatch, settings, gate=gate)
    doc = _doc(rag_space, DocumentStatus.READY)
    # La première exécution inline est retenue par la porte : la seconde requête doit
    # voir INGESTING et être refusée pendant que la première tourne encore.
    threading.Timer(1.5, gate.set).start()

    out = _two_simultaneous_posts(user, doc, body={"strategy": "standard"})

    codes = sorted(c for c, _ in out)
    assert codes == [200, 409], out
    rejected = next(b for c, b in out if c == 409)
    assert rejected["status"] == DocumentStatus.INGESTING
    assert len(calls) == 1 and calls[0]["status_at_start"] == DocumentStatus.INGESTING
    assert Document.objects.get(id=doc.id).status == DocumentStatus.READY


@pytest.mark.django_db(transaction=True)
def test_force_makes_concurrency_explicit(user, member, rag_space, settings, monkeypatch):
    """Une requête normale et une requête ``force=true`` simultanées peuvent TOUTES
    DEUX être admises : ``force`` rend la concurrence explicite (sûre par 1F)."""
    gate = threading.Event()
    calls = _capture_sync(monkeypatch, settings, gate=gate)
    doc = _doc(rag_space, DocumentStatus.READY)
    from rest_framework.test import APIClient

    first_started = threading.Event()
    real = views_write.reindex_existing_document

    def _signal(*, document_id, **kwargs):
        first_started.set()
        return real(document_id=document_id, **kwargs)

    monkeypatch.setattr(views_write, "reindex_existing_document", _signal)
    out: list = [None, None]

    def _normal():
        try:
            c = APIClient()
            c.force_authenticate(user=user)
            r = c.post(_url(doc), data={"strategy": "standard"}, format="json")
            out[0] = (r.status_code, r.json())
        finally:
            connection.close()

    def _forced():
        try:
            assert first_started.wait(timeout=15)
            c = APIClient()
            c.force_authenticate(user=user)
            r = c.post(_url(doc), data={"strategy": "standard", "force": True}, format="json")
            out[1] = (r.status_code, r.json())
            gate.set()
        finally:
            connection.close()

    ts = [threading.Thread(target=_normal), threading.Thread(target=_forced)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)

    assert out[0][0] == 200 and out[1][0] == 200, out
    assert len(calls) == 2
    assert Document.objects.get(id=doc.id).status == DocumentStatus.READY


# --------------------------------------------------------------------------- #
# G6 — chemins internes inchangés (aucune garde hors HTTP explicite)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [DocumentStatus.PENDING, DocumentStatus.INGESTING])
def test_g6_worker_ingestion_on_in_progress_document_is_not_guarded(rag_space, settings, monkeypatch, status):
    import apps.rag_core.services.ingest_document as ing
    from apps.rag_core import tasks

    doc = _doc(rag_space, status)
    seen: list = []

    def _fake(*, document_id, **kwargs):
        seen.append(Document.objects.get(id=document_id).status)
        Document.objects.filter(id=document_id).update(status=DocumentStatus.READY)
        return IngestResult(document_id=document_id, status=DocumentStatus.READY, chunks_created=1, deduped=False)

    monkeypatch.setattr(ing, "reindex_existing_document", _fake)
    fake_self = SimpleNamespace(max_retries=3, request=SimpleNamespace(id="t-g6", retries=0), retry=lambda **k: None)

    out = tasks._run_ingestion(fake_self, document_id=str(doc.id), request_id="rid", log_prefix="test-g6")

    assert out["ok"] is True and seen == [DocumentStatus.INGESTING], "la tâche pose INGESTING puis exécute, sans 409"


# --------------------------------------------------------------------------- #
# G7 — item de job actif : 409 avec identifiants ; force admis
# --------------------------------------------------------------------------- #
def _running_item(rag_space, user, doc, *, fresh=True):
    job = RagSpaceReindexJob.objects.create(
        rag_space=rag_space, requested_by=user, status=ReindexJobStatus.RUNNING, total_count=1, pending_count=0,
    )
    lease = timezone.now() + (timedelta(minutes=10) if fresh else -timedelta(minutes=10))
    item = RagSpaceReindexJobItem.objects.create(
        job=job, document=doc, status=ReindexItemStatus.RUNNING, started_at=timezone.now(),
        lease_expires_at=lease, celery_task_id="celery-secret-id",
    )
    return job, item


def test_g7_active_job_item_is_reported_without_task_id(api_client, user, rag_space, settings, monkeypatch):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.INGESTING)
    job, item = _running_item(rag_space, user, doc)

    resp = api_client.post(_url(doc) + "?async=true", data={}, format="json")

    assert resp.status_code == 409
    body = resp.json()
    assert body["active_job_id"] == str(job.id) and body["active_item_id"] == str(item.id)
    assert "celery" not in resp.content.decode().lower()
    assert calls == []


def test_g7_stale_lease_item_is_not_reported_as_active(api_client, user, rag_space, settings, monkeypatch):
    _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.INGESTING)
    _running_item(rag_space, user, doc, fresh=False)

    resp = api_client.post(_url(doc) + "?async=true", data={}, format="json")

    assert resp.status_code == 409
    assert "active_job_id" not in resp.json() and "active_item_id" not in resp.json()


def test_g7_force_with_active_item_is_admitted_and_does_not_touch_item(api_client, user, rag_space, settings, monkeypatch):
    calls = _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.INGESTING)
    job, item = _running_item(rag_space, user, doc)

    resp = api_client.post(_url(doc) + "?async=true", data={"force": True}, format="json")

    assert resp.status_code == 200 and len(calls) == 1
    assert RagSpaceReindexJobItem.objects.get(id=item.id).status == ReindexItemStatus.RUNNING


# --------------------------------------------------------------------------- #
# G8 — autorisation AVANT toute divulgation d'état
# --------------------------------------------------------------------------- #
def test_g8_user_without_access_learns_nothing(other_user, user, rag_space, settings, monkeypatch):
    from rest_framework.test import APIClient

    _capture_async(monkeypatch, settings)
    doc = _doc(rag_space, DocumentStatus.INGESTING)
    _running_item(rag_space, user, doc)
    client = APIClient()
    client.force_authenticate(user=other_user)

    resp = client.post(_url(doc) + "?async=true", data={}, format="json")

    assert resp.status_code in (403, 404)
    text = resp.content.decode().lower()
    assert "ingesting" not in text and "active_job_id" not in text and "active_item_id" not in text


# --------------------------------------------------------------------------- #
# Échec d'enqueue après admission : aucun PENDING bloqué
# --------------------------------------------------------------------------- #
def test_enqueue_failure_after_admission_does_not_leave_pending(api_client, rag_space, settings, monkeypatch):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    monkeypatch.setattr(views_write, "is_celery_eager_mode", lambda: False)
    monkeypatch.setattr(views_common, "broker_ready", lambda: False)
    doc = _doc(rag_space, DocumentStatus.READY)

    resp = api_client.post(_url(doc) + "?async=true", data={}, format="json")

    assert resp.status_code == 503
    assert Document.objects.get(id=doc.id).status == DocumentStatus.FAILED
