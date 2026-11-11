"""Audit persistant via les endpoints réels (delete/upload/reindex) — lot 5."""
from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.audit.models import ActionAuditEvent
from apps.rag_core.models import Document, RagSpace
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _events(action, resource_id=None, status=None):
    qs = ActionAuditEvent.objects.filter(action_name=action)
    if resource_id is not None:
        qs = qs.filter(resource_id=resource_id)
    if status is not None:
        qs = qs.filter(status=status)
    return list(qs.order_by("created_at"))


# ---- suppression ----------------------------------------------------------
def test_delete_success_audited(api_client, rag_space):
    doc = Document.objects.create(rag_space=rag_space, filename="d.pdf", status="READY")
    r = api_client.delete(f"/api/v1/documents/{doc.id}/delete/")
    assert r.status_code == 200
    names = [e.event_name for e in _events("document.delete", doc.id)]
    assert "action.started" in names and "action.completed" in names
    # aucun contenu sensible : filename NON stocké, metadata bornée
    completed = _events("document.delete", doc.id, "completed")[0]
    assert "d.pdf" not in str(completed.metadata)
    assert set(completed.metadata).issubset(
        # `storage_already_missing` distingue « supprimé » de « rien à supprimer » :
        # sans lui, l'audit d'une purge sur un espace dégradé affirmerait avoir
        # supprimé des fichiers qui n'étaient plus là.
        {"chunks_deleted", "storage_deleted", "storage_already_missing", "warnings"}
    )


def test_delete_denied_audited(api_client):
    # RagSpace d'un autre workspace : l'utilisateur (fixture) n'y est pas membre.
    ws = Workspace.objects.create(name="OW")
    rs = RagSpace.objects.create(name="other", workspace=ws)
    doc = Document.objects.create(rag_space=rs, filename="x.pdf", status="READY")
    r = api_client.delete(f"/api/v1/documents/{doc.id}/delete/")
    assert r.status_code == 404  # anti-énumération
    assert _events("document.delete", doc.id, "denied")


def test_delete_processing_audited(api_client, rag_space):
    doc = Document.objects.create(rag_space=rag_space, filename="p.pdf", status="PENDING")
    r = api_client.delete(f"/api/v1/documents/{doc.id}/delete/")
    assert r.status_code == 409
    failed = _events("document.delete", doc.id, "failed")
    assert failed and failed[0].error_code == "document_processing"


# ---- upload (sync en test : started + completed) --------------------------
def test_upload_success_audited(api_client, rag_space):
    f = SimpleUploadedFile("note.txt", b"Bonjour ceci est un document de test.", content_type="text/plain")
    r = api_client.post(
        "/api/v1/documents/upload/",
        {"file": f, "rag_space_id": str(rag_space.id)},
        format="multipart",
    )
    assert r.status_code in (200, 201)
    started = _events("document.upload", status="started")
    completed = _events("document.upload", status="completed")
    assert started and completed
    # métadonnées : mime/size/deduped, jamais le contenu
    md = completed[-1].metadata
    assert "mime_type" in md and "size_bytes" in md
    assert "Bonjour" not in str(md)


# ---- réindexation (doc sans storage -> started puis failed) ---------------
def test_reindex_started_and_failed_audited(api_client, rag_space):
    doc = Document.objects.create(rag_space=rag_space, filename="r.pdf", status="READY")
    r = api_client.post(f"/api/v1/documents/{doc.id}/reindex/")
    names = [e.event_name for e in _events("document.reindex", doc.id)]
    assert "action.detected" in names and "action.started" in names
    # Sans storage_file, la réindexation sync échoue -> événement failed. Elle
    # répond 409 avec le code d'indisponibilité : un 500 anonyme laissait
    # l'appelant sans savoir que le geste utile est de fournir la source.
    assert r.status_code == 409
    assert r.json()["error_code"] == "no_storage_file_for_reindex"
    assert r.json()["retryability"] == "replace_source_recommended"
    statuses = {e.status for e in _events("document.reindex", doc.id)}
    assert "failed" in statuses


def test_reindex_denied_audited(api_client):
    ws = Workspace.objects.create(name="OW2")
    rs = RagSpace.objects.create(name="o2", workspace=ws)
    doc = Document.objects.create(rag_space=rs, filename="z.pdf", status="READY")
    r = api_client.post(f"/api/v1/documents/{doc.id}/reindex/")
    assert r.status_code == 404
    assert _events("document.reindex", doc.id, "denied")
