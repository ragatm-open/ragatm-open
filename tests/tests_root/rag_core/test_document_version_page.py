"""CV-3B — modèles ``DocumentVersion`` / ``Page`` et helpers idempotents.

Aucun fichier, aucun raster : uniquement la base de test.
"""
from __future__ import annotations

import hashlib

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.rag_core.models import Document, DocumentStatus, DocumentVersion, Page, RagSpace
from apps.rag_core.models.document_version import ImmutableRowError
from apps.rag_core.services.documents.provenance import (
    ProvenanceConflict,
    ensure_document_version,
    ensure_pdf_pages,
)
from apps.rag_core.services.ingest.extraction.pdf_geometry import PdfPageGeometry
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

SHA_A = hashlib.sha256(b"octets A").hexdigest()
SHA_B = hashlib.sha256(b"octets B").hexdigest()


@pytest.fixture
def space(db):
    return RagSpace.objects.create(workspace=Workspace.objects.create(name="W"), name="S")


def _doc(space, sha=SHA_A, storage="rag_docs/s/d/doc.pdf", size=123, **kw):
    return Document.objects.create(rag_space=space, filename="doc.pdf", sha256=sha,
                                   size_bytes=size, storage_file=storage, **kw)


def _geo(n=2, **over):
    base = [PdfPageGeometry(page_number=i, mediabox=(0, 0, 595, 842), cropbox=(0, 0, 595, 842),
                            rotate=0) for i in range(1, n + 1)]
    return [PdfPageGeometry(**{**g.__dict__, **over}) if g.page_number == 1 and over else g for g in base]


# -- DocumentVersion ---------------------------------------------------------------
def test_version_created_from_document_with_exact_sha_size_and_logical_storage_key(space):
    doc = _doc(space)
    v = ensure_document_version(doc.id)
    assert v.document_id == doc.id
    assert v.content_sha256 == doc.sha256 == SHA_A
    assert v.size_bytes == 123
    assert v.storage_key == "rag_docs/s/d/doc.pdf"          # nom logique, jamais MEDIA_ROOT


def test_ensure_document_version_is_idempotent(space):
    doc = _doc(space)
    assert ensure_document_version(doc.id).id == ensure_document_version(doc.id).id
    assert DocumentVersion.objects.filter(document=doc).count() == 1


def test_same_sha_on_two_documents_is_allowed(space):
    other = RagSpace.objects.create(workspace=space.workspace, name="S2")
    v1 = ensure_document_version(_doc(space).id)
    v2 = ensure_document_version(_doc(other).id)
    assert v1.content_sha256 == v2.content_sha256 and v1.id != v2.id


def test_unique_document_and_sha(space):
    doc = _doc(space)
    ensure_document_version(doc.id)
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentVersion.objects.create(document=doc, content_sha256=SHA_A, size_bytes=1, storage_key="x")


def test_document_supports_several_versions(space):
    doc = _doc(space)
    DocumentVersion.objects.create(document=doc, content_sha256=SHA_A, size_bytes=1, storage_key="a")
    DocumentVersion.objects.create(document=doc, content_sha256=SHA_B, size_bytes=2, storage_key="b")
    assert doc.versions.count() == 2


@pytest.mark.parametrize("sha,storage", [("", "rag_docs/x"), (SHA_A, None), (SHA_A, ""), ("abc", "rag_docs/x")])
def test_no_version_without_authoritative_data(space, sha, storage):
    doc = _doc(space, sha=sha, storage=storage, status=DocumentStatus.FAILED)
    assert ensure_document_version(doc.id) is None
    assert not DocumentVersion.objects.filter(document=doc).exists()


def test_missing_document_returns_none():
    import uuid

    assert ensure_document_version(uuid.uuid4()) is None


def test_storage_key_drift_for_same_bytes_is_a_conflict_not_an_update(space):
    doc = _doc(space)
    v = ensure_document_version(doc.id)
    Document.objects.filter(id=doc.id).update(storage_file="rag_docs/elsewhere/doc.pdf")
    with pytest.raises(ProvenanceConflict) as e:
        ensure_document_version(doc.id)
    assert e.value.code == "document_version_conflict"
    v.refresh_from_db()
    assert v.storage_key == "rag_docs/s/d/doc.pdf"


def test_version_is_append_only(space):
    v = ensure_document_version(_doc(space).id)
    v.storage_key = "autre"
    with pytest.raises(ImmutableRowError):
        v.save()
    with pytest.raises(ImmutableRowError):
        DocumentVersion.objects.filter(id=v.id).update(content_sha256=SHA_B)
    with pytest.raises(ImmutableRowError):
        DocumentVersion.objects.bulk_update([v], ["storage_key"])
    v.refresh_from_db()
    assert (v.content_sha256, v.storage_key) == (SHA_A, "rag_docs/s/d/doc.pdf")


def test_version_rejects_malformed_sha_at_db_level(space):
    doc = _doc(space)
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentVersion.objects.bulk_create(
            [DocumentVersion(document=doc, content_sha256="ABC", size_bytes=1, storage_key="k")])


def test_document_delete_cascades_versions_and_pages(space):
    doc = _doc(space)
    v = ensure_document_version(doc.id)
    ensure_pdf_pages(v, _geo(2))
    doc.delete()
    assert not DocumentVersion.objects.filter(id=v.id).exists()
    assert not Page.objects.filter(document_version_id=v.id).exists()


# -- Page ----------------------------------------------------------------------
def test_pages_created_one_based_and_idempotent(space):
    v = ensure_document_version(_doc(space).id)
    first = ensure_pdf_pages(v, _geo(3))
    again = ensure_pdf_pages(v, _geo(3))
    assert [p.page_number for p in first] == [1, 2, 3]
    assert sorted(p.id for p in again) == sorted(p.id for p in first)
    assert Page.objects.filter(document_version=v).count() == 3


def test_different_geometry_for_same_version_is_a_conflict(space):
    v = ensure_document_version(_doc(space).id)
    ensure_pdf_pages(v, _geo(2))
    with pytest.raises(ProvenanceConflict) as e:
        ensure_pdf_pages(v, _geo(2, rotate=90))
    assert e.value.code == "page_geometry_conflict"
    with pytest.raises(ProvenanceConflict):
        ensure_pdf_pages(v, _geo(3))
    assert list(Page.objects.filter(document_version=v).values_list("rotate", flat=True)) == [0, 0]


def test_page_numbering_must_be_contiguous_from_one(space):
    v = ensure_document_version(_doc(space).id)
    gap = [PdfPageGeometry(page_number=2, mediabox=(0, 0, 1, 1), cropbox=(0, 0, 1, 1), rotate=0)]
    with pytest.raises(ProvenanceConflict):
        ensure_pdf_pages(v, gap)


def test_page_number_zero_rejected_by_model_and_db(space):
    v = ensure_document_version(_doc(space).id)
    with pytest.raises(ValidationError):
        Page.objects.create(document_version=v, page_number=0, mediabox=[0, 0, 1, 1],
                            cropbox=[0, 0, 1, 1], rotate=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        Page.objects.bulk_create([Page(document_version=v, page_number=0, mediabox=[0, 0, 1, 1],
                                       cropbox=[0, 0, 1, 1], rotate=0)])


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_valid_rotations_accepted(space, rotate):
    v = ensure_document_version(_doc(space).id)
    p = Page.objects.create(document_version=v, page_number=1, mediabox=[0, 0, 1, 1],
                            cropbox=[0, 0, 1, 1], rotate=rotate)
    assert Page.objects.get(id=p.id).rotate == rotate


@pytest.mark.parametrize("rotate", [45, 360, 91])
def test_invalid_rotation_rejected_by_model_and_db(space, rotate):
    v = ensure_document_version(_doc(space).id)
    with pytest.raises(ValidationError):
        Page.objects.create(document_version=v, page_number=1, mediabox=[0, 0, 1, 1],
                            cropbox=[0, 0, 1, 1], rotate=rotate)
    with pytest.raises(IntegrityError), transaction.atomic():
        Page.objects.bulk_create([Page(document_version=v, page_number=1, mediabox=[0, 0, 1, 1],
                                       cropbox=[0, 0, 1, 1], rotate=rotate)])


def test_unique_version_and_page_number_but_same_number_across_versions(space):
    v1 = ensure_document_version(_doc(space).id)
    other = RagSpace.objects.create(workspace=space.workspace, name="S2")
    v2 = ensure_document_version(_doc(other).id)
    kw = dict(mediabox=[0, 0, 1, 1], cropbox=[0, 0, 1, 1], rotate=0)
    Page.objects.create(document_version=v1, page_number=1, **kw)
    Page.objects.create(document_version=v2, page_number=1, **kw)
    with pytest.raises(IntegrityError), transaction.atomic():
        Page.objects.bulk_create([Page(document_version=v1, page_number=1, **kw)])


def test_page_label_nullable_and_display_only(space):
    v = ensure_document_version(_doc(space).id)
    p = Page.objects.create(document_version=v, page_number=1, page_label=None,
                            mediabox=[0, 0, 1, 1], cropbox=[0, 0, 1, 1], rotate=0)
    assert Page.objects.get(id=p.id).page_label is None


def test_boxes_round_trip_without_loss(space):
    v = ensure_document_version(_doc(space).id)
    box = [-10.5, 0, 595.2756, 841.8898]
    crop = [0.1, 0.2, 594.99999, 841.0000001]
    p = Page.objects.create(document_version=v, page_number=1, mediabox=box, cropbox=crop, rotate=0)
    stored = Page.objects.get(id=p.id)
    assert stored.mediabox == box and stored.cropbox == crop


@pytest.mark.parametrize("box", [[0, 0, 0, 10], [10, 0, 5, 10], [0, 0, 1], ["0", 0, 1, 1], [0, 0, 1, float("inf")],
                                 [True, 0, 1, 1]])
def test_invalid_boxes_rejected(space, box):
    v = ensure_document_version(_doc(space).id)
    with pytest.raises(ValidationError):
        Page.objects.create(document_version=v, page_number=1, mediabox=box, cropbox=[0, 0, 1, 1], rotate=0)


def test_page_is_append_only(space):
    v = ensure_document_version(_doc(space).id)
    (p,) = ensure_pdf_pages(v, _geo(1))
    p.rotate = 90
    with pytest.raises(ImmutableRowError):
        p.save()
    with pytest.raises(ImmutableRowError):
        Page.objects.filter(id=p.id).update(rotate=180)
