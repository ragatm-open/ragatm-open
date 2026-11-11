# tests/tests_root/http_api/test_documents_extraction_meta.py
"""
Tests P1 — exposition SÛRE de meta.extraction dans la sérialisation document.

Vérifie le sous-statut dérivé (success / ready_with_warnings / ocr_required) et
qu'aucun texte extrait n'est exposé.
"""
from __future__ import annotations

import pytest

from api.v1.documents.views_common import _serialize_document
from apps.rag_core.models import Document
from apps.rag_core.models.document import DocumentStatus

pytestmark = pytest.mark.django_db


def _doc(rag_space, *, status, error="", extraction=None):
    return Document.objects.create(
        rag_space=rag_space,
        filename="f.pdf",
        content_type="application/pdf",
        size_bytes=1,
        sha256="",
        status=status,
        error_message=error,
        meta={"extraction": extraction} if extraction is not None else {},
    )


def test_ready_success_substatus(rag_space):
    doc = _doc(
        rag_space, status=DocumentStatus.READY,
        extraction={
            "extraction_status": "success", "method": "pdf:pypdf",
            "pdf_native_engine": "pypdf", "pdf_native_fallback_used": False,
            "pages": 19, "text_pages": list(range(2, 20)), "empty_pages": [],
            "native_text_chars": 17000, "ocr_used": False,
        },
    )
    out = _serialize_document(doc)
    assert out["extraction"]["status"] == "success"
    assert out["extraction"]["method"] == "pdf:pypdf"
    assert out["extraction"]["native_fallback_used"] is False
    # aucun texte extrait exposé
    assert "text" not in out["extraction"]


def test_ready_with_warnings_when_empty_pages(rag_space):
    doc = _doc(
        rag_space, status=DocumentStatus.READY,
        extraction={
            "extraction_status": "success", "method": "pdf:pypdf",
            "pages": 19, "text_pages": list(range(2, 20)), "empty_pages": [1],
            "ocr_used": False,
        },
    )
    out = _serialize_document(doc)
    assert out["extraction"]["status"] == "ready_with_warnings"
    assert out["extraction"]["image_only_pages"] == [1]


def test_fallback_engine_exposed(rag_space):
    doc = _doc(
        rag_space, status=DocumentStatus.READY,
        extraction={
            "extraction_status": "success", "method": "pdf:pymupdf",
            "pdf_native_engine": "pymupdf", "pdf_native_fallback_used": True,
            "native_fallback_engine": "pymupdf", "empty_pages": [],
            "ocr_used": False,
        },
    )
    out = _serialize_document(doc)
    assert out["extraction"]["native_fallback_used"] is True
    assert out["extraction"]["native_fallback_engine"] == "pymupdf"


def test_failed_ocr_required_substatus(rag_space):
    doc = _doc(
        rag_space, status=DocumentStatus.FAILED,
        error="pdf_scanned_requires_ocr: PDF scanné (image) : aucun texte extractible.",
        extraction={"extraction_status": "failed", "error_code": "pdf_scanned_requires_ocr"},
    )
    out = _serialize_document(doc)
    assert out["extraction"]["status"] == "ocr_required"


def test_image_only_plan_exposes_page_placeholders(rag_space):
    """Plan image-only (P0) : sous-statut ocr_required + métadonnées page-level
    préservées (is_image_only_pdf, page_count) exposées à l'UX."""
    doc = _doc(
        rag_space, status=DocumentStatus.FAILED,
        error="pdf_scanned_requires_ocr: PDF graphique ou scanné.",
        extraction={
            "extraction_status": "failed", "error_code": "pdf_scanned_requires_ocr",
            "is_image_only_pdf": True, "page_count": 50,
            "image_only_pages": list(range(1, 51)), "native_text_chars": 7,
        },
    )
    out = _serialize_document(doc)
    assert out["extraction"]["status"] == "ocr_required"
    assert out["extraction"]["is_image_only_pdf"] is True
    assert out["extraction"]["page_count"] == 50


def test_no_extraction_meta_returns_no_key(rag_space):
    doc = _doc(rag_space, status=DocumentStatus.PENDING)
    out = _serialize_document(doc)
    assert "extraction" not in out
