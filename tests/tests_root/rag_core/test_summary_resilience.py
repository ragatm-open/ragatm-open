"""Tests de résilience des résumés (Priorité C) : reaper, retry, versioning cache."""
import uuid
from types import SimpleNamespace

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry
from django.test import override_settings
from django.utils import timezone

from apps.rag_core.models import Document, DocumentStatus, RagSpace
from apps.rag_core.models.summary import DocumentSummary
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _doc():
    ws = Workspace.objects.create(name="W")
    space = RagSpace.objects.create(name="S", workspace=ws)
    return Document.objects.create(rag_space=space, filename="d.pdf", status=DocumentStatus.READY), space


def _summary(document, space, *, status="PENDING", variant="bullets:document::fr"):
    return DocumentSummary.objects.create(
        rag_space=space,
        document=document,
        variant_key=variant,
        mode="bullets",
        scope="document",
        language="fr",
        status=status,
    )


# ---------------------------------------------------------------- reaper (item 3)

def _age(row, seconds):
    """Force updated_at dans le passé (update() ne déclenche pas auto_now)."""
    past = timezone.now() - timezone.timedelta(seconds=seconds)
    DocumentSummary.objects.filter(id=row.id).update(updated_at=past)


def test_reaper_marks_old_pending_failed():
    from apps.rag_core.services.summary_service import reap_stale_summaries

    doc, space = _doc()
    stale = _summary(doc, space, status="PENDING", variant="a")
    fresh = _summary(doc, space, status="PENDING", variant="b")
    ready = _summary(doc, space, status="READY", variant="c")
    _age(stale, 1200)  # 20 min
    _age(fresh, 60)    # 1 min

    reaped = reap_stale_summaries(timeout_s=900)
    assert reaped == 1

    stale.refresh_from_db()
    fresh.refresh_from_db()
    ready.refresh_from_db()
    assert stale.status == "FAILED"
    assert "stale_timeout" in stale.error_message
    assert fresh.status == "PENDING"   # trop récent
    assert ready.status == "READY"     # pas PENDING


def test_reaper_noop_when_nothing_stale():
    from apps.rag_core.services.summary_service import reap_stale_summaries

    doc, space = _doc()
    _summary(doc, space, status="PENDING", variant="a")  # frais
    assert reap_stale_summaries(timeout_s=900) == 0


def test_reaper_command():
    from django.core.management import call_command

    doc, space = _doc()
    stale = _summary(doc, space, status="PENDING")
    _age(stale, 2000)
    call_command("reap_stale_summaries", timeout=900)
    stale.refresh_from_db()
    assert stale.status == "FAILED"


# ---------------------------------------------------- versioning cache (item 1)

def test_prompt_version_changes_fingerprint():
    from apps.rag_core.services import summary_service as svc

    chunks = [SimpleNamespace(id=uuid.uuid4(), text="abc"), SimpleNamespace(id=uuid.uuid4(), text="def")]

    with override_settings(SUMMARY_PROMPT_VERSION=1):
        fp1 = svc._fingerprint(chunks)
    with override_settings(SUMMARY_PROMPT_VERSION=2):
        fp2 = svc._fingerprint(chunks)

    assert fp1 != fp2  # bump de version => cache invalidé
    # stable à version constante
    with override_settings(SUMMARY_PROMPT_VERSION=1):
        assert svc._fingerprint(chunks) == fp1


# ------------------------------------------------------- retry du task (item 2)

def test_task_permanent_error_fails_without_retry(monkeypatch):
    from apps.rag_core import tasks
    from apps.rag_core.services.summary_service import DocumentHasNoContent

    doc, space = _doc()
    row = _summary(doc, space, status="PENDING")

    calls = {"n": 0}

    def boom(r, *, user_id=None):
        calls["n"] += 1
        raise DocumentHasNoContent("vide")

    # run_summary_generation_for_row est importé localement dans le task : on patche
    # la source.
    monkeypatch.setattr(
        "apps.rag_core.services.summary_service.run_summary_generation_for_row", boom
    )

    out = tasks.generate_document_summary_task.run(summary_id=str(row.id))
    assert out["error"] == "document_no_content"
    assert calls["n"] == 1  # aucun retry
    row.refresh_from_db()
    assert row.status == "FAILED"


def test_task_provider_error_exhausted_marks_failed(monkeypatch):
    from apps.rag_core import tasks
    from apps.rag_core.services.summary_service import SummaryProviderError

    doc, space = _doc()
    row = _summary(doc, space, status="PENDING")

    def boom(r, *, user_id=None):
        raise SummaryProviderError("LLM down")

    monkeypatch.setattr(
        "apps.rag_core.services.summary_service.run_summary_generation_for_row", boom
    )
    # simule l'épuisement des retries
    monkeypatch.setattr(
        tasks.generate_document_summary_task,
        "retry",
        lambda *a, **k: (_ for _ in ()).throw(MaxRetriesExceededError()),
    )

    out = tasks.generate_document_summary_task.run(summary_id=str(row.id))
    assert out["error"] == "summary_provider_unavailable"
    row.refresh_from_db()
    assert row.status == "FAILED"


def test_task_provider_error_requests_retry(monkeypatch):
    from apps.rag_core import tasks
    from apps.rag_core.services.summary_service import SummaryProviderError

    doc, space = _doc()
    row = _summary(doc, space, status="PENDING")

    def boom(r, *, user_id=None):
        raise SummaryProviderError("LLM down")

    monkeypatch.setattr(
        "apps.rag_core.services.summary_service.run_summary_generation_for_row", boom
    )

    def fake_retry(*a, **k):
        raise Retry("retrying")

    monkeypatch.setattr(tasks.generate_document_summary_task, "retry", fake_retry)

    with pytest.raises(Retry):
        tasks.generate_document_summary_task.run(summary_id=str(row.id))

    # transitoire => on NE marque PAS FAILED (on retentera)
    row.refresh_from_db()
    assert row.status == "PENDING"
