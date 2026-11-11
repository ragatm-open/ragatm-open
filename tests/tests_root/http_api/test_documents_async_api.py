"""Tests du comportement **asynchrone** de l'upload, eager ET non-eager.

Rappel des contrats (3 tickets) :

- **Eager** (``CELERY_TASK_ALWAYS_EAGER=1``, dev) : pas de broker/worker. Un upload
  demandé en async est exécuté en **sync explicite** : réponse ``queued: false`` +
  document avec statut réel (READY/FAILED). JAMAIS ``queued: true``, JAMAIS de
  ``503 ingest_enqueue_unavailable`` pour un timeout provider.
- **Non-eager** (prod / dev async réel) : ``.delay()`` est observable.
  ``.delay()`` OK -> ``queued: true`` + doc PENDING ; broker KO -> ``503``.

Comme la suite tourne par défaut en eager (.env), on force explicitement le mode
voulu via la fixture ``eager`` / ``non_eager`` pour ne pas dépendre de l'env.
"""
import io
from unittest import mock

import pytest
from celery.exceptions import Retry

from api.v1.documents import views_common
import apps.rag_core.services.ingest.indexing as indexing
from apps.rag_core.models import Chunk, Document

pytestmark = pytest.mark.django_db


def _txt(content=b"Paris est la capitale de la France. Contenu suffisant pour 1 chunk."):
    f = io.BytesIO(content)
    f.name = "note.txt"
    return f


def _upload(api_client, rag_space, qs="", content=None, **extra):
    data = {"rag_space_id": str(rag_space.id), "file": _txt() if content is None else _txt(content)}
    return api_client.post(
        f"/api/v1/documents/upload/{qs}",
        data=data,
        format="multipart",
        **extra,
    )


@pytest.fixture
def eager(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    return settings


@pytest.fixture
def non_eager(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    return settings


@pytest.fixture
def broker_ok(monkeypatch):
    monkeypatch.setattr(views_common, "broker_ready", lambda: True)


@pytest.fixture
def spy_delay(monkeypatch):
    """Spy sur ``ingest_document_task.delay`` : la vraie tâche ne tourne pas."""
    spy = mock.Mock()
    monkeypatch.setattr(views_common.ingest_document_task, "delay", spy)
    return spy


def _embeddings_raise(message):
    def _fn(texts, dim=None, **kwargs):
        raise RuntimeError(message)

    return _fn


# ===========================================================================
# EAGER : async neutralisé en sync explicite (Option C)
# ===========================================================================
def test_eager_async_runs_sync_ready_no_delay(api_client, rag_space, eager, spy_delay):
    """?async=true en eager => sync : queued:false, doc READY, chunks, .delay non appelé."""
    res = _upload(api_client, rag_space, "?async=true")
    assert res.status_code == 200, res.data
    assert res.data["queued"] is False
    doc_id = res.data["document"]["id"]
    assert Document.objects.get(id=doc_id).status == "READY"
    assert Chunk.objects.filter(document_id=doc_id).count() >= 1
    assert spy_delay.call_count == 0  # jamais enfilé en eager


def test_eager_async_provider_timeout_no_503_marks_failed(api_client, rag_space, eager, spy_delay, monkeypatch):
    """Timeout provider en eager => PAS de 503 enqueue. queued:false + doc FAILED honnête."""
    monkeypatch.setattr(indexing, "embed_texts_with_identity", _embeddings_raise("503 provider unavailable (timeout)"))

    res = _upload(api_client, rag_space, "?async=true")
    assert res.status_code == 200, res.data          # surtout PAS 503
    assert res.data["queued"] is False
    doc_id = res.data["document"]["id"]
    doc = Document.objects.get(id=doc_id)
    assert doc.status == "FAILED"                     # ni PENDING ni INGESTING bloqué
    assert "transient_ingest_error" in (doc.error_message or "")
    assert "enqueue" not in (doc.error_message or "")  # message honnête (pas enqueue)
    assert spy_delay.call_count == 0


def test_eager_async_business_error_empty_marks_failed(api_client, rag_space, eager, spy_delay):
    """Erreur métier (document vide) en eager => queued:false + doc FAILED, pas de retry parasite."""
    res = _upload(api_client, rag_space, "?async=true", content=b"   \n  ")
    assert res.status_code == 200, res.data
    assert res.data["queued"] is False
    doc = Document.objects.get(id=res.data["document"]["id"])
    assert doc.status == "FAILED"
    assert spy_delay.call_count == 0


def test_eager_async_dedup_when_ready(api_client, rag_space, eager, spy_delay):
    """Dédup (doc déjà prêt) court-circuite avant toute décision async/sync."""
    first = _upload(api_client, rag_space, "?async=false")
    assert first.status_code == 200
    second = _upload(api_client, rag_space, "?async=true")
    assert second.status_code == 200, second.data
    assert second.data.get("deduped") is True
    assert spy_delay.call_count == 0
    assert Document.objects.filter(rag_space=rag_space).count() == 1


# ---- sync explicite (?async=false / setting) : inchangé en eager ----
def test_sync_false_runs_inline_ready(api_client, rag_space, eager):
    res = _upload(api_client, rag_space, "?async=false")
    assert res.status_code == 200, res.data
    assert res.data["queued"] is False
    assert Document.objects.get(id=res.data["document"]["id"]).status == "READY"


def test_default_sync_via_setting(api_client, rag_space, eager, settings):
    settings.RAG_INGEST_ASYNC_DEFAULT = False
    res = _upload(api_client, rag_space)
    assert res.status_code == 200, res.data
    assert res.data["queued"] is False
    assert Document.objects.get(id=res.data["document"]["id"]).status == "READY"


# ===========================================================================
# NON-EAGER : vrai async observable (Tickets 1 & 2 préservés)
# ===========================================================================
def test_noneager_async_posts_task_queued_pending(api_client, rag_space, non_eager, broker_ok, spy_delay):
    res = _upload(api_client, rag_space, "?async=true")
    assert res.status_code == 200, res.data
    assert res.data["queued"] is True
    doc_id = res.data["document_id"]
    assert spy_delay.call_count == 1
    assert spy_delay.call_args.kwargs.get("document_id") == doc_id
    assert Document.objects.get(id=doc_id).status == "PENDING"
    assert Chunk.objects.filter(document_id=doc_id).count() == 0


def test_noneager_default_async_via_setting(api_client, rag_space, non_eager, broker_ok, spy_delay, settings):
    settings.RAG_INGEST_ASYNC_DEFAULT = True
    res = _upload(api_client, rag_space)  # aucun param
    assert res.status_code == 200, res.data
    assert res.data["queued"] is True
    assert spy_delay.call_count == 1
    assert Document.objects.get(id=res.data["document_id"]).status == "PENDING"


def test_noneager_async_via_header(api_client, rag_space, non_eager, broker_ok, spy_delay, settings):
    settings.RAG_INGEST_ASYNC_DEFAULT = False
    res = _upload(api_client, rag_space, **{"HTTP_X_RAG_ASYNC": "true"})
    assert res.status_code == 200, res.data
    assert res.data["queued"] is True
    assert spy_delay.call_count == 1


def test_noneager_ocr_forces_async(api_client, rag_space, non_eager, broker_ok, spy_delay, settings):
    settings.DOCUMENT_OCR_ENABLED = True
    settings.RAG_INGEST_ASYNC_DEFAULT = False
    res = _upload(api_client, rag_space, "?async=false")  # OCR force quand même async
    assert res.status_code == 200, res.data
    assert res.data["queued"] is True
    assert spy_delay.call_count == 1
    assert Document.objects.get(id=res.data["document_id"]).status == "PENDING"


def test_noneager_broker_down_returns_503_and_marks_failed(api_client, rag_space, non_eager, monkeypatch):
    monkeypatch.setattr(views_common, "broker_ready", lambda: False)
    res = _upload(api_client, rag_space, "?async=true")
    assert res.status_code == 503, res.data
    assert res.data["queued"] is False
    assert res.data["detail"] == "ingest_enqueue_unavailable"
    doc = Document.objects.get(id=res.data["document_id"])
    assert doc.status == "FAILED"
    assert "enqueue_failed" in (doc.error_message or "")


def test_noneager_delay_broker_failure_returns_503(api_client, rag_space, non_eager, broker_ok, monkeypatch):
    """Vrai échec broker/transport sur .delay() => 503 ingest_enqueue_unavailable + FAILED."""
    from kombu.exceptions import OperationalError

    def _boom(*args, **kwargs):
        raise OperationalError("redis connection refused")

    monkeypatch.setattr(views_common.ingest_document_task, "delay", _boom)
    res = _upload(api_client, rag_space, "?async=true")
    assert res.status_code == 503, res.data
    assert res.data.get("queued") is False
    assert res.data["detail"] == "ingest_enqueue_unavailable"
    assert Document.objects.get(id=res.data["document_id"]).status == "FAILED"


# ===========================================================================
# Option D (défensif) : Retry n'est pas un échec d'enqueue
# ===========================================================================
def test_enqueue_ingest_reraises_celery_retry_not_failed(rag_space, monkeypatch):
    """Si .delay() lève celery.exceptions.Retry (eager mal routé), _enqueue_ingest
    relaie l'exception et ne marque PAS le doc FAILED (ce n'est pas un échec broker)."""
    doc = Document.objects.create(
        rag_space=rag_space, filename="d.txt", content_type="text/plain",
        size_bytes=1, sha256="retrycase", status="PENDING",
    )

    def _raise_retry(*args, **kwargs):
        raise Retry()

    monkeypatch.setattr(views_common.ingest_document_task, "delay", _raise_retry)
    with pytest.raises(Retry):
        views_common._enqueue_ingest(document_id=str(doc.id))

    doc.refresh_from_db()
    assert doc.status == "PENDING"  # pas FAILED, pas classé enqueue_failed
