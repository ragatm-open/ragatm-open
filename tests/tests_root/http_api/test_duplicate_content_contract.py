"""même RagSpace + même sha256 = même identité de contenu.

Un nouvel import byte-identique ne crée JAMAIS un second RagDocument, y compris quand
l'occurrence existante est FAILED : cette ligne est RÉUTILISÉE comme cible de reprise
(même document_id). Un document actif (PENDING/INGESTING/READY) est rendu tel quel, comme
avant. La contrainte DB `uniq_rag_document_space_sha256_not_failed` reste la garde de
course ; une collision à la transition FAILED → INGESTING est un échec MÉTIER typé
`duplicate_content`, non retryable, jamais un IntegrityError brut.

On compte les ENFILEMENTS réels (`ingest_document_task.delay`) et les LIGNES, pas les
réponses HTTP seules.
"""
import hashlib
import io
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.urls import reverse

from api.v1.documents import views_common, views_write
from apps.audit.models import ActionAuditEvent
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.repositories import document_repository as repo
from apps.rag_core.services.ingest import pipeline, reindex
from apps.rag_core.services.ingest.retry_strategy import NON_RETRYABLE, classify_retryability
from apps.rag_core.services.ingest.types import (
    DUPLICATE_CONTENT,
    DuplicateContentError,
    is_retryable_error,
)

pytestmark = pytest.mark.django_db

CONTENT = b"Cahier des clauses techniques particulieres. Lot 02 charpente metallique."
OTHER = b"Bordereau des prix unitaires. Lot 05 menuiseries exterieures."
SHA = hashlib.sha256(CONTENT).hexdigest()


def _upload(api_client, space, content=CONTENT, name="cctp.txt", async_=True):
    f = io.BytesIO(content)
    f.name = name
    url = "/api/v1/documents/upload/" + ("?async=true" if async_ else "?async=false")
    return api_client.post(url, data={"rag_space_id": str(space.id), "file": f}, format="multipart")


@pytest.fixture
def non_eager(settings, tmp_path):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.MEDIA_ROOT = str(tmp_path / "media")
    return settings


@pytest.fixture
def broker_ok(monkeypatch):
    monkeypatch.setattr(views_common, "broker_ready", lambda: True)


@pytest.fixture
def spy_delay(monkeypatch):
    spy = mock.Mock()
    monkeypatch.setattr(views_common.ingest_document_task, "delay", spy)
    return spy


def _rows(space):
    return Document.objects.filter(rag_space=space).count()


def _stored(space, content=CONTENT, name="cctp.txt", status=DocumentStatus.FAILED, error=""):
    doc = Document.objects.create(
        rag_space=space, filename=name, content_type="text/plain", size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(), status=status, error_message=error,
    )
    doc.storage_file.save(name, ContentFile(content), save=True)
    return doc


# =========================================================================
# §27 — FAILED ré-uploadé : la ligne est réutilisée
# =========================================================================
class TestFailedReupload:
    def test_failed_reupload_reuses_row_same_id_no_second_row(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        first = _upload(api_client, rag_space)
        assert first.status_code == 200 and first.data["deduped"] is False
        doc_id = first.data["document_id"]
        Document.objects.filter(id=doc_id).update(
            status=DocumentStatus.FAILED, error_message="pipeline_id 'emb-v2:x' incohérent avec task='document'",
        )
        assert spy_delay.call_count == 1

        second = _upload(api_client, rag_space, name="autre-nom.txt")
        assert second.status_code == 200, second.data
        assert second.data["document_id"] == doc_id              # DOCUMENT_ID_BEFORE == DOCUMENT_ID_AFTER
        assert second.data["deduped"] is True
        assert second.data["queued"] is True
        assert second.data["detail"] == views_write.FAILED_DOCUMENT_RETRIED
        assert _rows(rag_space) == 1                               # RAG_DOCUMENT_ROW_DELTA = 0
        assert spy_delay.call_count == 2                           # FAILED_REUSE_JOB_COUNT = 1 par ré-upload
        doc = Document.objects.get(id=doc_id)
        assert doc.filename == "cctp.txt"                          # provenance d'origine conservée
        assert doc.storage_file.name                               # source conservée
        ev = ActionAuditEvent.objects.filter(resource_id=doc_id, action_name="document.upload", status="queued").order_by("-created_at").first()
        assert ev is not None and ev.metadata.get("reuse") == views_write.FAILED_DOCUMENT_RETRIED

    def test_failed_reupload_restores_missing_source_file(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        """Un FAILED sans fichier (échec de stockage) retrouve sa source au ré-upload."""
        doc = Document.objects.create(rag_space=rag_space, filename="cctp.txt", content_type="text/plain",
                                      size_bytes=len(CONTENT), sha256=SHA, status=DocumentStatus.FAILED,
                                      error_message="storage_file_save_failed: disk")
        res = _upload(api_client, rag_space)
        assert res.status_code == 200 and res.data["document_id"] == str(doc.id)
        doc.refresh_from_db()
        assert doc.storage_file.name and _rows(rag_space) == 1

    def test_sync_pipeline_reingests_failed_row_once(self, rag_space, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path / "media")
        doc = _stored(rag_space, error="pipeline_id 'emb-v2:x' incohérent avec task='document'")
        res = pipeline.ingest_document_from_upload(
            rag_space_id=rag_space.id, filename="copie.txt", content_type="text/plain",
            file_bytes=CONTENT, async_mode=False,
        )
        assert res.document_id == str(doc.id) and res.retried is True and res.deduped is True
        assert res.status == DocumentStatus.READY
        assert _rows(rag_space) == 1
        chunks = list(Chunk.objects.filter(document=doc).values_list("chunk_index", flat=True))
        assert chunks and len(chunks) == len(set(chunks))         # NO_DUPLICATE_FRAGMENTS
        assert Chunk.objects.filter(document=doc, embedding__isnull=True).count() == 0  # NO_DUPLICATE_VECTORS / tous vectorisés
        assert Document.objects.get(id=doc.id).error_message == ""


# =========================================================================
# §28, §29 — document actif : comportement historique conservé
# =========================================================================
class TestActiveDuplicate:
    def test_ready_duplicate_returns_existing_no_row_no_job(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        doc = _stored(rag_space, status=DocumentStatus.READY)
        Chunk.objects.create(rag_space=rag_space, document=doc, chunk_index=0, text="x", embedding=[0.0] * 768, meta={})
        res = _upload(api_client, rag_space, name="encore.txt")
        assert res.status_code == 200 and res.data["deduped"] is True
        assert res.data["document"]["id"] == str(doc.id)
        assert _rows(rag_space) == 1
        assert spy_delay.call_count == 0                           # READY_DUPLICATE_JOB_COUNT = 0
        assert Chunk.objects.filter(document=doc).count() == 1     # aucun nouveau fragment

    def test_ingesting_duplicate_not_enqueued_no_row(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        doc_id = _upload(api_client, rag_space).data["document_id"]
        Document.objects.filter(id=doc_id).update(status=DocumentStatus.INGESTING)
        res = _upload(api_client, rag_space)
        assert res.data["detail"] == views_write.INGESTION_ALREADY_IN_PROGRESS
        assert res.data["document_id"] == doc_id and _rows(rag_space) == 1 and spy_delay.call_count == 1


# =========================================================================
# §30, §31, §32 — frontières de l'identité de contenu
# =========================================================================
class TestContentIdentityBoundaries:
    def test_same_sha_different_space_allowed(self, rag_space, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path / "media")
        other = RagSpace.objects.create(name="autre", workspace=rag_space.workspace)
        a = pipeline.ingest_document_from_upload(rag_space_id=rag_space.id, filename="a.txt", content_type="text/plain", file_bytes=CONTENT, async_mode=False)
        b = pipeline.ingest_document_from_upload(rag_space_id=other.id, filename="a.txt", content_type="text/plain", file_bytes=CONTENT, async_mode=False)
        assert a.document_id != b.document_id and b.deduped is False
        assert _rows(rag_space) == 1 and _rows(other) == 1

    def test_different_content_same_name_same_space_two_documents(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        a = _upload(api_client, rag_space, content=CONTENT, name="foo.txt").data["document_id"]
        b = _upload(api_client, rag_space, content=OTHER, name="foo.txt").data["document_id"]
        assert a != b and _rows(rag_space) == 2

    def test_different_name_same_bytes_is_one_document(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        a = _upload(api_client, rag_space, name="A.txt").data
        b = _upload(api_client, rag_space, name="B.txt").data
        assert a["document_id"] == b["document_id"] and _rows(rag_space) == 1
        assert Document.objects.get(id=a["document_id"]).filename == "A.txt"   # pas de nouvelle sémantique de nom


# =========================================================================
# §33 — plusieurs FAILED historiques : première occurrence réutilisée, déterministe
# =========================================================================
class TestHistoricalMultiFailed:
    def test_oldest_failed_is_reused_others_untouched(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        a = _stored(rag_space, name="Image-1.png", error="pipeline_id 'emb-v2:x' incohérent avec task='document'")
        b = _stored(rag_space, name="Image-2.png", error="pipeline_id 'emb-v2:x' incohérent avec task='document'")
        assert _rows(rag_space) == 2                                # la contrainte autorise N FAILED identiques
        res = _upload(api_client, rag_space, name="Image-3.png")
        assert res.status_code == 200 and res.data["document_id"] == str(a.id)   # created_at puis id croissants
        assert res.data["detail"] == views_write.FAILED_DOCUMENT_RETRIED
        assert _rows(rag_space) == 2                                # aucune ligne créée
        b.refresh_from_db()
        assert b.status == DocumentStatus.FAILED and b.ingest_started_at is None   # non touché
        assert repo.find_existing_by_sha(space=rag_space, sha256=SHA).id == a.id

    def test_active_wins_over_failed_in_lookup(self, rag_space):
        failed = _stored(rag_space, name="old.txt")
        ready = _stored(rag_space, name="new.txt", status=DocumentStatus.READY)
        assert repo.find_existing_by_sha(space=rag_space, sha256=SHA).id == ready.id
        assert repo.find_active_duplicate(space_id=rag_space.id, sha256=SHA, exclude_id=failed.id).id == ready.id
        assert repo.find_active_duplicate(space_id=rag_space.id, sha256=SHA, exclude_id=ready.id) is None


# =========================================================================
# §35, §36 — collision EXACTE à la transition FAILED → INGESTING
# =========================================================================
class TestExactConstraintCollision:
    @pytest.fixture(autouse=True)
    def _media(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path / "media")

    def test_reindex_of_failed_twin_is_typed_duplicate_content(self, rag_space):
        ready = _stored(rag_space, name="ready.txt", status=DocumentStatus.READY)
        Chunk.objects.create(rag_space=rag_space, document=ready, chunk_index=0, text="x", embedding=[0.0] * 768, meta={})
        failed = _stored(rag_space, name="twin.txt", error="pipeline_id 'emb-v2:x' incohérent avec task='document'")
        with pytest.raises(DuplicateContentError) as exc:
            reindex.reindex_existing_document(document_id=str(failed.id))
        assert exc.value.duplicate_of == str(ready.id)
        failed.refresh_from_db()
        assert failed.status == DocumentStatus.FAILED                      # atomicité CLEAN
        assert failed.error_message.startswith(DUPLICATE_CONTENT)
        assert "uniq_rag_document" not in failed.error_message             # pas de SQL exposé
        assert failed.meta["duplicate_of"] == str(ready.id)
        assert failed.meta["extraction"]["error_code"] == DUPLICATE_CONTENT
        assert Chunk.objects.filter(document=failed).count() == 0
        assert Chunk.objects.filter(document=ready).count() == 1           # le gagnant est intact
        assert _rows(rag_space) == 2
        # non retryable : ni marqueur transitoire (Celery), ni stratégie de reprise
        assert not is_retryable_error(exc.value)
        assert classify_retryability(failed)[0] == NON_RETRYABLE

    def test_task_path_returns_duplicate_content_once_no_retry(self, rag_space):
        from apps.rag_core import tasks

        ready = _stored(rag_space, name="ready.txt", status=DocumentStatus.READY)
        failed = _stored(rag_space, name="twin.txt", error="x")

        class _Req:
            id = "task-dup"; retries = 0

        class _Task:
            max_retries = 3; request = _Req

            def retry(self, exc=None, countdown=None, **kw):  # pragma: no cover
                raise AssertionError("un duplicate_content ne doit jamais être retenté")

        out = tasks._run_ingestion(_Task(), document_id=str(failed.id), request_id="dup", log_prefix="dup")
        assert out["ok"] is False and out["error"] == DUPLICATE_CONTENT and out["duplicate_of"] == str(ready.id)
        failed.refresh_from_db()
        assert failed.status == DocumentStatus.FAILED and Chunk.objects.filter(document=failed).count() == 0
        events = list(ActionAuditEvent.objects.filter(resource_id=failed.id, action_name="document.ingestion", status="failed"))
        assert len(events) == 1 and events[0].error_code == DUPLICATE_CONTENT   # DUPLICATE_CONTENT_MAX_JOB_CREATION = 1

    def test_unrelated_integrity_error_is_not_converted(self, rag_space, monkeypatch):
        failed = _stored(rag_space, name="alone.txt")

        class _QS:
            def update(self, **kw):
                raise IntegrityError('duplicate key value violates unique constraint "some_other_constraint"')

        class _Objects:
            def select_related(self, *a):
                return Document.objects.select_related(*a)

            def filter(self, **kw):
                return _QS()

        class _Doc:
            objects = _Objects()

        monkeypatch.setattr(reindex, "Document", _Doc)
        with pytest.raises(IntegrityError):
            reindex.reindex_existing_document(document_id=str(failed.id))

    def test_collision_predicate_is_exact(self):
        assert repo.is_duplicate_content_collision(IntegrityError(f'duplicate key value violates unique constraint "{repo.DUPLICATE_CONTENT_CONSTRAINT}"'))
        assert not repo.is_duplicate_content_collision(IntegrityError('duplicate key value violates unique constraint "other"'))
        assert not repo.is_duplicate_content_collision(ValueError(repo.DUPLICATE_CONTENT_CONSTRAINT))

        class _Diag:
            constraint_name = "other_constraint"

        class _Cause(Exception):
            diag = _Diag()

        exc = IntegrityError(f"text mentions {repo.DUPLICATE_CONTENT_CONSTRAINT}")
        exc.__cause__ = _Cause()
        assert not repo.is_duplicate_content_collision(exc)        # le diagnostic PostgreSQL prime sur le texte

    def test_create_or_get_reraises_unrelated_integrity_error(self, rag_space, monkeypatch):
        def _boom(**kw):
            raise IntegrityError('null value in column "rag_space_id" violates not-null constraint')

        monkeypatch.setattr(repo.Document.objects, "create", _boom)
        with pytest.raises(IntegrityError):
            repo.create_or_get_by_sha(space=rag_space, filename="x.txt", content_type="text/plain", size_bytes=1, sha256=SHA)


# =========================================================================
# §39 — supprimer puis réimporter reste possible
# =========================================================================
class TestDeleteThenReimport:
    def test_reimport_after_delete_creates_fresh_document(self, api_client, rag_space, non_eager, broker_ok, spy_delay):
        first = _upload(api_client, rag_space).data["document_id"]
        Document.objects.filter(id=first).update(status=DocumentStatus.READY)
        resp = api_client.delete(reverse("documents-delete", args=[first]))
        assert resp.status_code in (200, 204), resp.data
        assert _rows(rag_space) == 0
        second = _upload(api_client, rag_space).data
        assert second["deduped"] is False and second["document_id"] != first and _rows(rag_space) == 1


class TestRetryClassificationIgnoresIdentifiers:
    """Défaut constaté en CI (05/11/2026) : l'UUID du document en double contenait « 429 », et l'erreur DÉFINITIVE
    était classée transitoire. Les identifiants ne sont jamais des codes HTTP."""

    @pytest.mark.parametrize("uuid", ["93c879f3-9d59-4b4d-b0d9-8acb045429a9", "50300000-0000-4000-8000-000000000504",
                                      "00000000-0000-4000-8502-000000000000"])
    def test_duplicate_content_is_never_retryable(self, uuid):
        from apps.rag_core.services.ingest.types import DuplicateContentError, is_retryable_error

        assert is_retryable_error(DuplicateContentError(uuid)) is False

    def test_identifier_in_a_generic_error_is_not_a_status_code(self):
        from apps.rag_core.services.ingest.types import is_retryable_error

        assert is_retryable_error(ValueError("chunk 3f2a5034296b8c1d0e9f invalide")) is False
        assert is_retryable_error(ValueError("upstream returned 429 Too Many Requests")) is True
        assert is_retryable_error(RuntimeError("HTTP 503 Service Unavailable")) is True
