"""Événements worker document.ingestion + corrélation request_id/task_id (§10-11)."""
from __future__ import annotations

import pytest

from apps.audit.models import ActionAuditEvent

pytestmark = pytest.mark.django_db


class _FakeSelf:
    max_retries = 3

    class request:
        id = "task-abc-123"


@pytest.fixture
def doc_with_storage(db):
    from django.contrib.auth import get_user_model  # noqa: F401

    from apps.rag_core.models import Document, RagSpace
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(name="W")
    rs = RagSpace.objects.create(name="S", workspace=ws)
    # storage_file non vide -> _has_storage_file True (aucun fichier réel requis pour ce test).
    return Document.objects.create(rag_space=rs, filename="d.pdf", status="PENDING", storage_file="rag_docs/x/d.pdf")


def test_ingestion_started_completed_correlated(doc_with_storage, monkeypatch):
    import apps.rag_core.services.ingest_document as ing
    from apps.rag_core import tasks

    class _Res:
        document_id = str(doc_with_storage.id)
        status = "READY"
        chunks_created = 3

    monkeypatch.setattr(ing, "reindex_existing_document", lambda **k: _Res())

    out = tasks._run_ingestion(
        _FakeSelf(), document_id=str(doc_with_storage.id), request_id="rid-777", log_prefix="test",
    )
    assert out["ok"] is True

    evs = list(
        ActionAuditEvent.objects.filter(action_name="document.ingestion", resource_id=doc_with_storage.id)
        .order_by("created_at")
    )
    names = [e.event_name for e in evs]
    assert "action.started" in names and "action.completed" in names
    # corrélation : même request_id + task_id sur tous les événements worker
    for e in evs:
        assert e.request_id == "rid-777"
        assert e.task_id == "task-abc-123"
    completed = [e for e in evs if e.event_name == "action.completed"][0]
    assert completed.metadata.get("chunk_count") == 3


def test_ingestion_failed_no_storage(db, monkeypatch):
    from apps.rag_core import tasks
    from apps.rag_core.models import Document, RagSpace
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(name="W2")
    rs = RagSpace.objects.create(name="S2", workspace=ws)
    doc = Document.objects.create(rag_space=rs, filename="n.pdf", status="PENDING")  # pas de storage_file

    tasks._run_ingestion(_FakeSelf(), document_id=str(doc.id), request_id="rid-9", log_prefix="test")
    assert ActionAuditEvent.objects.filter(
        action_name="document.ingestion", status="failed", error_code="no_storage_file", resource_id=doc.id
    ).exists()
