"""F5-A : après un échec définitif d'ingestion, le document passe à FAILED (jamais
bloqué en INGESTING) ; un échec de la transition FAILED est lui-même journalisé, la
cause initiale n'est pas perdue, aucun contenu documentaire n'est loggé."""
from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.django_db

SECRET = "SECRET_SENTINEL_DO_NOT_LOG"


class _FakeSelf:
    max_retries = 3

    class request:
        id = "task-1"
        retries = 3  # retries épuisés -> erreur finale


@pytest.fixture
def doc(db):
    from apps.rag_core.models import Document, RagSpace
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(name="W")
    rs = RagSpace.objects.create(name="S", workspace=ws)
    # SECRET dans le nom de fichier (contenu métier) : ne doit JAMAIS apparaître dans
    # les logs (qui n'émettent que document_id).
    return Document.objects.create(
        rag_space=rs, filename=f"{SECRET}.pdf", status="INGESTING", storage_file="rag_docs/x/d.pdf"
    )


def test_definitive_failure_marks_document_failed(doc, monkeypatch):
    import apps.rag_core.services.ingest_document as ing
    from apps.rag_core import tasks
    from apps.rag_core.services.ingest.types import RetryableIngestError  # noqa: F401

    # étape d'ingestion en échec NON transitoire -> FAILED définitif
    def boom(**k):
        raise ing.IngestError("corrupted document")  # message dev, sans contenu

    monkeypatch.setattr(ing, "reindex_existing_document", boom)
    tasks._run_ingestion(_FakeSelf(), document_id=str(doc.id), request_id="rid", log_prefix="test")

    doc.refresh_from_db()
    assert doc.status == "FAILED"  # jamais bloqué en INGESTING


def test_failed_transition_error_is_logged_and_primary_cause_kept(doc, monkeypatch, caplog):
    import apps.rag_core.services.ingest_document as ing
    from apps.rag_core import tasks

    def boom(**k):
        raise ing.IngestError("corrupted")

    monkeypatch.setattr(ing, "reindex_existing_document", boom)

    # la transition -> FAILED échoue AUSSI (DB indisponible sur l'update)
    from apps.rag_core.models import Document

    real_filter = Document.objects.filter

    def flaky_filter(*a, **kw):
        qs = real_filter(*a, **kw)
        orig_update = qs.update

        def update(**uk):
            if uk.get("status") == "FAILED":
                raise RuntimeError("db down")
            return orig_update(**uk)

        qs.update = update
        return qs

    monkeypatch.setattr(Document.objects, "filter", flaky_filter)

    with caplog.at_level(logging.WARNING, logger="apps.rag_core.tasks"):
        tasks._run_ingestion(_FakeSelf(), document_id=str(doc.id), request_id="rid", log_prefix="test")

    # 2e log distinct : impossible de marquer FAILED
    assert "unable to mark document FAILED" in caplog.text
    # cause initiale conservée (log final d'ingestion)
    assert "ingest error (final)" in caplog.text
    # document_id présent, aucun contenu documentaire loggé
    assert str(doc.id) in caplog.text
    assert SECRET not in caplog.text


def test_no_content_in_ingestion_logs(doc, monkeypatch, caplog):
    import apps.rag_core.services.ingest_document as ing
    from apps.rag_core import tasks

    monkeypatch.setattr(ing, "reindex_existing_document", lambda **k: (_ for _ in ()).throw(ing.IngestError("format invalide")))
    with caplog.at_level(logging.WARNING, logger="apps.rag_core.tasks"):
        tasks._run_ingestion(_FakeSelf(), document_id=str(doc.id), request_id="rid", log_prefix="test")
    # le nom de fichier (contenu métier) n'est jamais loggé (seul document_id l'est)
    assert SECRET not in caplog.text
