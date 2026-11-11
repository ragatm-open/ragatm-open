"""Phase 5 — Ingestion des documents de corpus métier.

Réutilise les garde-fous d'ingestion du privé (validation MIME/allowlist, refus
exe/macro/archive, limites) ; cible d'écriture = DomainCorpusDocument/Chunk.
La réindexation ne touche JAMAIS rag_document / rag_chunk.
"""
import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.corpus.models import DomainCorpusChunk, DomainCorpusDocument
from apps.corpus.models.document import CorpusDocumentStatus
from apps.corpus.services.ingest import (
    ingest_corpus_document_from_upload,
    reindex_corpus,
)

pytestmark = pytest.mark.django_db

TXT = b"Ceci est un document de test sur la logistique, les normes et la reglementation."


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "hello")
    return buf.getvalue()


def _docm_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types macroEnabled='1'/>")
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/vbaProject.bin", b"\x00\x01")
    return buf.getvalue()


def _upload(client, corpus_id, *, name, content, content_type):
    f = SimpleUploadedFile(name, content, content_type=content_type)
    return client.post(
        f"/api/v1/admin/domain-corpora/{corpus_id}/documents/",
        {"file": f},
        format="multipart",
    )


# ---------------------------------------------------------------------------
# Upload autorisé / refus
# ---------------------------------------------------------------------------
def test_upload_txt_ready_with_chunks(global_owner_client, domain_corpus):
    res = _upload(global_owner_client, domain_corpus.id, name="reg.txt", content=TXT, content_type="text/plain")
    assert res.status_code == 201, res.data
    doc_id = res.data["document"]["id"]
    doc = DomainCorpusDocument.objects.get(id=doc_id)
    assert doc.status == CorpusDocumentStatus.READY
    assert DomainCorpusChunk.objects.filter(document_id=doc_id).count() >= 1


def test_upload_executable_refused(global_owner_client, domain_corpus):
    res = _upload(global_owner_client, domain_corpus.id, name="evil.exe",
                  content=b"MZ" + b"\x00" * 256, content_type="application/octet-stream")
    assert res.status_code == 400
    assert DomainCorpusDocument.objects.filter(corpus=domain_corpus).count() == 0


def test_upload_archive_refused(global_owner_client, domain_corpus):
    res = _upload(global_owner_client, domain_corpus.id, name="a.zip",
                  content=_zip_bytes(), content_type="application/zip")
    assert res.status_code == 400
    assert DomainCorpusDocument.objects.filter(corpus=domain_corpus).count() == 0


def test_upload_macro_doc_refused(global_owner_client, domain_corpus):
    res = _upload(global_owner_client, domain_corpus.id, name="m.docm",
                  content=_docm_bytes(), content_type="application/octet-stream")
    assert res.status_code == 400
    assert DomainCorpusDocument.objects.filter(corpus=domain_corpus).count() == 0


def test_workspace_admin_cannot_upload(ws_admin, domain_corpus):
    res = _upload(ws_admin["client"], domain_corpus.id, name="reg.txt", content=TXT, content_type="text/plain")
    assert res.status_code == 403
    assert DomainCorpusDocument.objects.filter(corpus=domain_corpus).count() == 0


# ---------------------------------------------------------------------------
# Dédup sha256
# ---------------------------------------------------------------------------
def test_dedup_sha256_same_corpus(domain_corpus):
    r1 = ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="a.txt", content_type="text/plain", file_bytes=TXT
    )
    r2 = ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="a-copy.txt", content_type="text/plain", file_bytes=TXT
    )
    assert r1.document_id == r2.document_id
    assert r2.deduped is True
    assert DomainCorpusDocument.objects.filter(corpus=domain_corpus).count() == 1


def test_same_document_allowed_in_two_corpora(domain_corpus, business_domain):
    from apps.corpus.models import DomainCorpus

    other = DomainCorpus.objects.create(domain=business_domain, slug="autre", name="Autre")
    r1 = ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="a.txt", content_type="text/plain", file_bytes=TXT
    )
    r2 = ingest_corpus_document_from_upload(
        corpus=other, filename="a.txt", content_type="text/plain", file_bytes=TXT
    )
    assert r1.document_id != r2.document_id
    assert r2.deduped is False


# ---------------------------------------------------------------------------
# Réindexation : isolation vs privé
# ---------------------------------------------------------------------------
def test_reindex_corpus_does_not_touch_private(domain_corpus, rag_space, ingest_text):
    from apps.rag_core.models import Chunk, Document

    # 1) document privé indexé
    ingest_text(rag_space, "Document prive du workspace A.", filename="prive.txt")
    private_docs_before = Document.objects.count()
    private_chunks_before = Chunk.objects.count()
    assert private_chunks_before >= 1

    # 2) document de corpus
    ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="reg.txt", content_type="text/plain", file_bytes=TXT
    )

    # 3) réindexation du corpus
    summary = reindex_corpus(corpus=domain_corpus)
    assert summary["reindexed"] == 1

    # 4) le privé est strictement inchangé
    assert Document.objects.count() == private_docs_before
    assert Chunk.objects.count() == private_chunks_before
