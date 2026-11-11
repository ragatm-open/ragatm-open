"""Tests de la tâche d'ingestion asynchrone (apps.rag_core.tasks._run_ingestion).

Deux familles :

1. Tests "câblage tâche" : mockent ``reindex_existing_document`` pour valider la
   logique retry/non-retry/échec final de ``_run_ingestion``/``_handle_ingest_failure``.
2. Tests "vrai chemin" (ticket 2) : mockent ``embed_texts_with_identity`` (couture
   provider d'embeddings depuis le lot 3 — elle rend vecteurs ET identité de pipeline)
   et exécutent la VRAIE chaîne reindex -> indexing, pour prouver qu'une erreur
   transitoire d'embeddings remonte bien en ``RetryableIngestError`` et déclenche
   le retry Celery — pas seulement un mock artificiel de reindex.
"""
from types import SimpleNamespace

import pytest
from django.core.files.base import ContentFile

import apps.rag_core.tasks as tasks
import apps.rag_core.services.ingest.indexing as indexing
from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest.reindex import reindex_existing_document
from apps.rag_core.services.ingest.types import IngestError, IngestResult

pytestmark = pytest.mark.django_db


class _FakeRetry(Exception):
    pass


class _FakeSelf:
    """Imite l'API Celery bind=True (self.request.retries, self.max_retries, self.retry)."""

    def __init__(self, retries=0, max_retries=3):
        self.max_retries = max_retries
        self.request = SimpleNamespace(retries=retries)
        self.retried_with = None

    def retry(self, *, exc=None, countdown=None):
        self.retried_with = {"exc": exc, "countdown": countdown}
        raise _FakeRetry()


@pytest.fixture
def doc(rag_space):
    d = Document.objects.create(
        rag_space=rag_space,
        filename="d.txt",
        content_type="text/plain",
        size_bytes=10,
        sha256="deadbeef",
        status=DocumentStatus.PENDING,
    )
    return d


@pytest.fixture(autouse=True)
def _force_storage_file(monkeypatch):
    # On considère le storage_file présent (sinon erreur permanente immédiate).
    monkeypatch.setattr(tasks, "_has_storage_file", lambda _doc: True)
    monkeypatch.setattr(tasks, "_best_effort_extract_and_store_doc_date", lambda _doc: None)


def _patch_reindex(monkeypatch, fn):
    import apps.rag_core.services.ingest_document as ing

    monkeypatch.setattr(ing, "reindex_existing_document", fn)


def test_success_marks_ready(monkeypatch, doc):
    def _ok(*, document_id, allow_retry=False, ocr_enabled=None, extraction_timeout_seconds=None):
        Document.objects.filter(id=document_id).update(status=DocumentStatus.READY)
        return IngestResult(document_id=str(document_id), status="READY", chunks_created=3)

    _patch_reindex(monkeypatch, _ok)
    out = tasks._run_ingestion(_FakeSelf(), document_id=str(doc.id), request_id=None, log_prefix="t")
    assert out["ok"] is True
    assert out["status"] == "READY"
    doc.refresh_from_db()
    assert doc.status == "READY"


def test_transient_error_retries_and_sets_pending(monkeypatch, doc):
    def _boom(*, document_id, allow_retry=False, ocr_enabled=None, extraction_timeout_seconds=None):
        raise RuntimeError("503 Service Unavailable from embeddings provider")

    _patch_reindex(monkeypatch, _boom)
    self_ = _FakeSelf(retries=0, max_retries=3)
    with pytest.raises(_FakeRetry):
        tasks._run_ingestion(self_, document_id=str(doc.id), request_id=None, log_prefix="t")
    assert self_.retried_with is not None
    doc.refresh_from_db()
    # En attente de retry => PENDING (visible côté polling).
    assert doc.status == "PENDING"


def test_business_error_no_retry_marks_failed(monkeypatch, doc):
    def _bad(*, document_id, allow_retry=False, ocr_enabled=None, extraction_timeout_seconds=None):
        raise IngestError("unsupported_office_format")

    _patch_reindex(monkeypatch, _bad)
    self_ = _FakeSelf(retries=0, max_retries=3)
    out = tasks._run_ingestion(self_, document_id=str(doc.id), request_id=None, log_prefix="t")
    assert out["ok"] is False
    assert self_.retried_with is None  # pas de retry sur erreur métier
    doc.refresh_from_db()
    assert doc.status == "FAILED"
    assert "unsupported_office_format" in (doc.error_message or "")


def test_transient_error_final_failure_after_retries(monkeypatch, doc):
    def _boom(*, document_id, allow_retry=False, ocr_enabled=None, extraction_timeout_seconds=None):
        raise RuntimeError("connection reset by peer")

    _patch_reindex(monkeypatch, _boom)
    # retries == max_retries => plus de retry => FAILED final.
    self_ = _FakeSelf(retries=3, max_retries=3)
    out = tasks._run_ingestion(self_, document_id=str(doc.id), request_id=None, log_prefix="t")
    assert out["ok"] is False
    assert self_.retried_with is None
    doc.refresh_from_db()
    assert doc.status == "FAILED"


def test_no_storage_file_is_permanent_failure(monkeypatch, doc):
    monkeypatch.setattr(tasks, "_has_storage_file", lambda _doc: False)
    out = tasks._run_ingestion(_FakeSelf(), document_id=str(doc.id), request_id=None, log_prefix="t")
    assert out["ok"] is False
    assert out["error"] == "no_storage_file_for_reindex"
    doc.refresh_from_db()
    assert doc.status == "FAILED"


# ---------------------------------------------------------------------------
# Ticket 2 — VRAI chemin embeddings/provider (mock embed_texts, pas reindex)
# ---------------------------------------------------------------------------
@pytest.fixture
def doc_with_text(rag_space):
    """Document réel avec storage_file contenant du texte extractible."""
    d = Document.objects.create(
        rag_space=rag_space,
        filename="real.txt",
        content_type="text/plain",
        size_bytes=0,
        sha256="",
        status=DocumentStatus.PENDING,
    )
    d.storage_file.save(
        "real.txt",
        ContentFile(b"Paris est la capitale de la France. Texte suffisant pour produire des chunks."),
        save=True,
    )
    return d


def _embeddings_raise(message):
    def _fn(texts, dim=None, **kwargs):
        raise RuntimeError(message)

    return _fn


def test_real_embeddings_transient_error_triggers_retry(monkeypatch, doc_with_text):
    """la couture d'embeddings lève une erreur transitoire => RetryableIngestError remonte =>
    self.retry() est réellement appelé, le doc repasse PENDING (pas FAILED)."""
    # storage_file réel : on ne mocke PAS _has_storage_file ici.
    monkeypatch.setattr(tasks, "_has_storage_file", lambda _doc: True)
    monkeypatch.setattr(tasks, "_best_effort_extract_and_store_doc_date", lambda _doc: None)
    monkeypatch.setattr(indexing, "embed_texts_with_identity", _embeddings_raise("503 provider unavailable"))

    self_ = _FakeSelf(retries=0, max_retries=3)
    with pytest.raises(_FakeRetry):
        tasks._run_ingestion(self_, document_id=str(doc_with_text.id), request_id=None, log_prefix="t")

    assert self_.retried_with is not None  # retry réellement déclenché
    doc_with_text.refresh_from_db()
    assert doc_with_text.status == "PENDING"  # pas FAILED tant que retries non épuisés
    assert Chunk.objects.filter(document_id=doc_with_text.id).count() == 0


def test_real_embeddings_transient_error_final_failure(monkeypatch, doc_with_text):
    """Après épuisement des retries, l'erreur transitoire => FAILED + error_message sûr."""
    monkeypatch.setattr(tasks, "_has_storage_file", lambda _doc: True)
    monkeypatch.setattr(tasks, "_best_effort_extract_and_store_doc_date", lambda _doc: None)
    monkeypatch.setattr(indexing, "embed_texts_with_identity", _embeddings_raise("429 rate limit exceeded"))

    self_ = _FakeSelf(retries=3, max_retries=3)  # plus de retry possible
    out = tasks._run_ingestion(self_, document_id=str(doc_with_text.id), request_id=None, log_prefix="t")

    assert out["ok"] is False
    assert self_.retried_with is None
    doc_with_text.refresh_from_db()
    assert doc_with_text.status == "FAILED"
    # Message utile mais sans secret provider.
    assert "transient_ingest_error" in (doc_with_text.error_message or "")


def test_real_embeddings_error_sync_degrades_to_failed_no_raise(monkeypatch, doc_with_text):
    """En mode sync (allow_retry=False), une erreur transitoire d'embeddings est
    dégradée proprement en FAILED — pas d'exception, pas de retry, pas de 500."""
    monkeypatch.setattr(indexing, "embed_texts_with_identity", _embeddings_raise("503 provider unavailable"))

    res = reindex_existing_document(document_id=str(doc_with_text.id))  # allow_retry=False
    assert res.status == "FAILED"
    doc_with_text.refresh_from_db()
    assert doc_with_text.status == "FAILED"
    assert "transient_ingest_error" in (doc_with_text.error_message or "")


def test_real_business_error_empty_document_no_retry(monkeypatch, rag_space):
    """Erreur métier (document vide) => FAILED sans retry, même en async."""
    monkeypatch.setattr(tasks, "_has_storage_file", lambda _doc: True)
    monkeypatch.setattr(tasks, "_best_effort_extract_and_store_doc_date", lambda _doc: None)

    d = Document.objects.create(
        rag_space=rag_space, filename="empty.txt", content_type="text/plain",
        size_bytes=0, sha256="", status=DocumentStatus.PENDING,
    )
    d.storage_file.save("empty.txt", ContentFile(b"   \n  "), save=True)  # vide après strip

    self_ = _FakeSelf(retries=0, max_retries=3)
    out = tasks._run_ingestion(self_, document_id=str(d.id), request_id=None, log_prefix="t")

    # reindex a retourné un IngestResult(FAILED) SANS lever (erreur métier) : la
    # tentative est un échec (INGEST-1D), même sans exception ni retry.
    assert out["ok"] is False
    assert out["status"] == "FAILED"
    assert out["doc_status"] == "FAILED"
    assert self_.retried_with is None  # pas de retry sur erreur métier
    d.refresh_from_db()
    assert d.status == "FAILED"
    assert (d.error_message or "") != ""
