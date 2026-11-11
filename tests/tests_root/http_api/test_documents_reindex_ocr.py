# tests/tests_root/http_api/test_documents_reindex_ocr.py
"""
Tests P1 bis — endpoint reindex avec OCR à la demande.

Vérifie que `?ocr=true` force l'asynchrone et transmet le flag `ocr` à l'enqueue,
sans activer l'OCR globalement. On monkeypatche l'enqueue pour capter l'argument
(pas de broker requis).
"""
from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from api.v1.documents import views_common, views_write
from apps.rag_core.models import Document
from apps.rag_core.models.document import DocumentStatus

pytestmark = pytest.mark.django_db


def _doc(rag_space):
    doc = Document.objects.create(
        rag_space=rag_space,
        filename="plan.pdf",
        content_type="application/pdf",
        size_bytes=1,
        sha256="",
        status=DocumentStatus.FAILED,
        error_message="pdf_scanned_requires_ocr: ...",
    )
    doc.storage_file.save("plan.pdf", ContentFile(b"%PDF-1.4 dummy"), save=True)
    return doc


def test_reindex_ocr_true_forces_async_and_passes_flag(api_client, rag_space, monkeypatch):
    doc = _doc(rag_space)
    captured = {}

    # Pas de broker en test : on capte l'appel d'enqueue et son flag ocr.
    def _fake_enqueue(*, request, doc, ocr=None, extraction_timeout_seconds=None):
        captured["ocr"] = ocr
        captured["timeout"] = extraction_timeout_seconds
        return None  # succès (pas de 503)

    monkeypatch.setattr(views_write, "enqueue_async_or_503", _fake_enqueue)
    # OCR force l'async même si le mode eager est actif en test.
    monkeypatch.setattr(views_write, "is_celery_eager_mode", lambda: False)

    url = reverse("documents-reindex", args=[str(doc.id)])
    resp = api_client.post(url + "?ocr=true")

    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    assert resp.json()["ocr"] is True
    assert captured["ocr"] is True


def test_reindex_without_ocr_does_not_set_flag(api_client, rag_space, monkeypatch):
    doc = _doc(rag_space)
    captured = {}

    def _fake_enqueue(*, request, doc, ocr=None, extraction_timeout_seconds=None):
        captured["ocr"] = ocr
        captured["timeout"] = extraction_timeout_seconds
        return None

    monkeypatch.setattr(views_write, "enqueue_async_or_503", _fake_enqueue)
    monkeypatch.setattr(views_write, "is_celery_eager_mode", lambda: False)
    # async demandé explicitement (sans ocr)
    url = reverse("documents-reindex", args=[str(doc.id)])
    resp = api_client.post(url + "?async=true")

    assert resp.status_code == 200
    assert captured["ocr"] is None
