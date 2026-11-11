# tests/tests_root/rag_core/test_ocr_on_demand.py
"""
Tests P1 bis — OCR à la demande (local-only, sélectif) + rotation + métadonnées
page-level. On simule un PDF image-only (pypdf + fallbacks vides) et on
monkeypatche le moteur OCR (pas de tesseract requis pour ces tests).
"""
from __future__ import annotations

from io import BytesIO

import pytest

from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest import extractors, reindex_existing_document
from apps.rag_core.services.ingest.extraction import (
    ExtractionError,
    ExtractionStatus,
    extract_document,
)
from apps.rag_core.services.ingest.extraction import ocr as ocr_module
from apps.rag_core.services.ingest.extraction.ocr import OcrPage, OcrResult

pytestmark = pytest.mark.django_db


def _pdf_bytes(n_lines: int = 4) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for i in range(n_lines):
        c.drawString(72, y, f"ligne {i}")
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def _force_image_only(monkeypatch, n_pages: int = 3):
    """pypdf voit n_pages sans texte ; aucun moteur natif de secours ne récupère."""
    pages = [(i, "") for i in range(1, n_pages + 1)]
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes", lambda *a, **k: list(pages))
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})


def _fake_ocr(pages_text: dict[int, str], *, rotations=None, conf=72.0, partial=False):
    rotations = rotations or {}

    def _impl(raw, page_numbers, *, languages=None, page_timeout_s=None):
        res = OcrResult(used=True, partial=partial)
        for pn in page_numbers:
            res.pages.append(
                OcrPage(
                    page_number=pn,
                    text=pages_text.get(pn, ""),
                    confidence=conf,
                    rotation=rotations.get(pn, 0),
                )
            )
        return res

    return _impl


# ---------------------------------------------------------------------
# 1. OCR à la demande récupère un PDF image-only (+ rotation + métadonnées)
# ---------------------------------------------------------------------
def test_extract_with_ocr_recovers_image_only(monkeypatch):
    _force_image_only(monkeypatch, n_pages=2)
    monkeypatch.setattr(
        ocr_module, "ocr_pdf_pages",
        _fake_ocr(
            {1: "MASSIF M01 NAPPE INFERIEURE HA14 e=0.15", 2: "COUPE A-A HA20"},
            rotations={1: 90},
        ),
    )

    res = extract_document(
        _pdf_bytes(), filename="plan.pdf", content_type="application/pdf",
        ocr_enabled=True,
    )

    assert res.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)
    assert res.metadata["ocr_used"] is True
    assert res.method.endswith("+ocr")
    assert res.metadata["ocr_pages"] == [1, 2]
    assert res.metadata["page_rotations"] == {"1": 90}
    # Contrat de PROPAGATION (mode confiance ON) : l'OCR simulé fournit la confiance,
    # le provider réel n'est pas exercé ici. Le contrat du mode OFF — aucune confiance
    # fabriquée — vit dans test_ocr_optional_confidence.py.
    assert "1" in res.metadata["page_ocr_confidences"]
    assert "MASSIF M01" in res.text


# ---------------------------------------------------------------------
# 2. OCR activé mais aucun texte produit -> ocr_no_text (pas READY)
# ---------------------------------------------------------------------
def test_extract_with_ocr_no_text(monkeypatch):
    _force_image_only(monkeypatch, n_pages=2)
    monkeypatch.setattr(ocr_module, "ocr_pdf_pages", _fake_ocr({1: "", 2: ""}))

    with pytest.raises(ExtractionError) as exc:
        extract_document(
            _pdf_bytes(), filename="plan.pdf", content_type="application/pdf",
            ocr_enabled=True,
        )
    assert exc.value.code == "ocr_no_text"


def test_extract_with_ocr_engine_missing(monkeypatch):
    """Moteur/rastériseur OCR absent -> code distinct ocr_engine_missing (pas ocr_no_text)."""
    from apps.rag_core.services.ingest.extraction.ocr import OcrResult

    _force_image_only(monkeypatch, n_pages=2)

    def _no_engine(raw, page_numbers, *, languages=None, page_timeout_s=None):
        res = OcrResult(used=True, partial=True)
        res.warnings.append("ocr_pdf_rasterizer_missing")
        return res  # aucune page produite

    monkeypatch.setattr(ocr_module, "ocr_pdf_pages", _no_engine)

    with pytest.raises(ExtractionError) as exc:
        extract_document(
            _pdf_bytes(), filename="plan.pdf", content_type="application/pdf",
            ocr_enabled=True,
        )
    assert exc.value.code == "ocr_engine_missing"


# ---------------------------------------------------------------------
# 3. OCR partiel (1 page récupérée, 1 vide) -> exploitable (SUCCESS/PARTIAL)
# ---------------------------------------------------------------------
def test_extract_with_ocr_partial(monkeypatch):
    _force_image_only(monkeypatch, n_pages=2)
    monkeypatch.setattr(
        ocr_module, "ocr_pdf_pages",
        _fake_ocr({1: "MASSIF M02 HA20 récupéré par OCR sur la page", 2: ""}, partial=True),
    )

    res = extract_document(
        _pdf_bytes(), filename="plan.pdf", content_type="application/pdf",
        ocr_enabled=True,
    )
    assert res.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)
    assert res.metadata["ocr_pages"] == [1]
    assert res.metadata["ocr_failed_pages"] == [2]
    assert "MASSIF M02" in res.text


# ---------------------------------------------------------------------
# 4. Réindexation OCR à la demande : image-only -> READY (sans OCR global)
# ---------------------------------------------------------------------
def _make_doc_with_pdf(rag_space) -> Document:
    from django.core.files.base import ContentFile

    doc = Document.objects.create(
        rag_space=rag_space,
        filename="plan.pdf",
        content_type="application/pdf",
        size_bytes=1,
        sha256="",
        status=DocumentStatus.FAILED,
        error_message="pdf_scanned_requires_ocr: ...",
    )
    doc.storage_file.save("plan.pdf", ContentFile(_pdf_bytes()), save=True)
    return doc


def test_reindex_with_ocr_on_demand_makes_ready(rag_space, monkeypatch):
    doc = _make_doc_with_pdf(rag_space)
    _force_image_only(monkeypatch, n_pages=2)
    monkeypatch.setattr(
        ocr_module, "ocr_pdf_pages",
        _fake_ocr({1: "MASSIF M01 HA14 e=0.15 nappe inferieure", 2: "COUPE B-B HA20"}),
    )

    res = reindex_existing_document(document_id=str(doc.id), ocr_enabled=True)
    assert res.status == DocumentStatus.READY
    assert res.chunks_created >= 1

    doc.refresh_from_db()
    assert doc.status == DocumentStatus.READY
    assert doc.meta["extraction"]["ocr_used"] is True
    assert doc.meta["extraction"]["ocr_pages"] == [1, 2]
    assert Chunk.objects.filter(document=doc).count() >= 1


def test_reindex_without_ocr_stays_image_only(rag_space, monkeypatch):
    """Sans OCR (flag absent + OCR global off), un image-only reste FAILED/OCR requis."""
    doc = _make_doc_with_pdf(rag_space)
    _force_image_only(monkeypatch, n_pages=2)
    # On s'assure que l'OCR n'est PAS tenté (aucun moteur monkeypatché requis).

    res = reindex_existing_document(document_id=str(doc.id), ocr_enabled=None)
    assert res.status == DocumentStatus.FAILED

    doc.refresh_from_db()
    assert "pdf_scanned_requires_ocr" in doc.error_message
    assert doc.meta["extraction"]["is_image_only_pdf"] is True
    assert Chunk.objects.filter(document=doc).count() == 0
