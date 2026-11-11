# tests/tests_root/http_api/test_documents_delete.py
"""Tests API — suppression définitive d'un document (P / cleanup).

Couvre : owner supprime FAILED/READY, cascade chunks + résumés, fichier source,
permissions (viewer 403, autre workspace 404 anti-énumération), 409 ingestion,
idempotence (404), fichier physique absent (pas de 500)."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import reverse

from apps.rag_core.models import Chunk, Document, DocumentSummary
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Membership, Workspace
from apps.rag_core.models import RagSpace

pytestmark = pytest.mark.django_db


def _doc(rag_space, *, status=DocumentStatus.READY, with_file=True, with_chunks=0):
    doc = Document.objects.create(
        rag_space=rag_space,
        filename="plan.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256="",
        status=status,
    )
    if with_file:
        doc.storage_file.save("plan.pdf", ContentFile(b"%PDF-1.4 dummy"), save=True)
    for i in range(with_chunks):
        Chunk.objects.create(
            rag_space=rag_space, document=doc, chunk_index=i,
            text=f"chunk {i}", embedding=[0.0] * 768, page_number=1, meta={},
        )
    return doc


def _url(doc):
    return reverse("documents-delete", args=[str(doc.id)])


def test_owner_deletes_failed_document(api_client, rag_space):
    doc = _doc(rag_space, status=DocumentStatus.FAILED, with_chunks=0)
    file_name = doc.storage_file.name
    storage = doc.storage_file.storage

    resp = api_client.delete(_url(doc))
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["file_deleted"] is True
    assert not Document.objects.filter(id=doc.id).exists()
    assert not storage.exists(file_name)


def test_owner_deletes_ready_with_chunks_and_summary(api_client, rag_space):
    doc = _doc(rag_space, status=DocumentStatus.READY, with_chunks=3)
    DocumentSummary.objects.create(
        rag_space=rag_space, document=doc, variant_key="default", scope="document",
        status="READY", summary="résumé", source_fingerprint="x",
    )
    resp = api_client.delete(_url(doc))
    assert resp.status_code == 200
    assert resp.json()["chunks_deleted"] == 3
    assert not Document.objects.filter(id=doc.id).exists()
    assert Chunk.objects.filter(document_id=doc.id).count() == 0
    assert DocumentSummary.objects.filter(document_id=doc.id).count() == 0  # cascade


def test_viewer_cannot_delete(rag_space, workspace):
    viewer = get_user_model().objects.create_user(username="viewer", password="pw")
    Membership.objects.create(workspace=workspace, user=viewer, role=Membership.ROLE_VIEWER)
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=viewer)
    doc = _doc(rag_space, status=DocumentStatus.READY)
    resp = client.delete(_url(doc))
    assert resp.status_code == 403
    assert Document.objects.filter(id=doc.id).exists()


def test_other_workspace_is_not_found(api_client, rag_space):
    # Document dans un AUTRE workspace auquel l'utilisateur n'a pas accès.
    other_ws = Workspace.objects.create(name="W2")
    other_space = RagSpace.objects.create(name="S2", workspace=other_ws)
    doc = _doc(other_space, status=DocumentStatus.READY)
    resp = api_client.delete(_url(doc))
    assert resp.status_code == 404  # anti-énumération
    assert Document.objects.filter(id=doc.id).exists()


@pytest.mark.parametrize("st", [DocumentStatus.PENDING, DocumentStatus.INGESTING])
def test_processing_document_is_conflict(api_client, rag_space, st):
    doc = _doc(rag_space, status=st)
    resp = api_client.delete(_url(doc))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "document_processing"
    assert Document.objects.filter(id=doc.id).exists()


def test_delete_is_idempotent_404(api_client, rag_space):
    doc = _doc(rag_space, status=DocumentStatus.FAILED)
    url = _url(doc)
    assert api_client.delete(url).status_code == 200
    # 2e suppression -> 404 (et pas de 500)
    assert api_client.delete(url).status_code == 404


def test_missing_physical_file_is_controlled(api_client, rag_space):
    doc = _doc(rag_space, status=DocumentStatus.FAILED, with_file=True)
    # On supprime le fichier physique sous le pied, la ligne DB garde le name.
    name = doc.storage_file.name
    doc.storage_file.storage.delete(name)
    resp = api_client.delete(_url(doc))
    assert resp.status_code == 200  # pas de 500
    assert resp.json()["deleted"] is True
    assert not Document.objects.filter(id=doc.id).exists()
