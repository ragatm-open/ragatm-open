# tests/tests_root/rag_core/test_table_ingestion.py
"""Tests P3-a2 — branchement des tableaux structurés en chunks ATOMIQUES.

Le 1er test est hermétique (monkeypatch de l'extraction pdfplumber) : il valide le
CÂBLAGE (chunk-tableau créé, meta JSON, non fragmenté) sans dépendre de pdfplumber.
Le 2e est un bout-en-bout réel gardé par importorskip(pdfplumber+reportlab).
"""
from __future__ import annotations

from io import BytesIO

import pytest

from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest import ingest_document_from_upload
from apps.rag_core.services.ingest.extraction import table_extractor as TX

pytestmark = pytest.mark.django_db


def _text_pdf(text="Note de calcul — annexe efforts.") -> bytes:
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def _fake_pile_table():
    rows = [
        ["Noeud/Cas", "FX [T]", "FY [T]", "FZ [T]", "MX [Tm]", "MY [Tm]", "MZ [Tm]"],
        ["153/ELU+", "-11,4", "-3,1", "-160", "0", "0", "0"],
        ["150/ELU-", "-0,2", "15,5", "54,8", "0", "0", "0"],
    ]
    return TX.build_table_from_rows(rows, page_number=18)


def test_table_chunk_created_atomic(rag_space, monkeypatch):
    # Le PDF a du texte natif (1 page) -> PdfExtractor tente l'extraction tables ;
    # on monkeypatch pdfplumber pour renvoyer un pile_head_forces déterministe.
    table = _fake_pile_table()
    monkeypatch.setattr(TX, "extract_tables_from_pdf_bytes", lambda data, pages=None: [table])

    res = ingest_document_from_upload(
        rag_space_id=rag_space.id,
        filename="note.pdf",
        content_type="application/pdf",
        file_bytes=_text_pdf(),
        async_mode=False,
    )
    assert res.status == DocumentStatus.READY

    doc = Document.objects.get(id=res.document_id)
    table_chunks = [c for c in Chunk.objects.filter(document=doc) if c.meta.get("kind") == "table"]
    assert len(table_chunks) == 1
    tc = table_chunks[0]
    # chunk ATOMIQUE : 1 seul chunk pour tout le tableau
    assert tc.meta["table_type"] == "pile_head_forces"
    assert tc.page_number == 18
    assert tc.meta["table"]["rows"][0]["node"] == "153"
    assert tc.meta["table"]["rows"][0]["fz"] == -160.0
    assert tc.meta["requires_visual_check"] is False
    # texte = Markdown embeddable (recherche possible)
    assert "Efforts en têtes de pieux" in tc.text and "153" in tc.text

    # index léger exposé dans meta.extraction
    idx = doc.meta["extraction"]["tables"]
    assert idx and idx[0]["table_type"] == "pile_head_forces" and idx[0]["rows"] == 2


def test_no_tables_when_none_extracted(rag_space, monkeypatch):
    monkeypatch.setattr(TX, "extract_tables_from_pdf_bytes", lambda data, pages=None: [])
    res = ingest_document_from_upload(
        rag_space_id=rag_space.id, filename="note.pdf",
        content_type="application/pdf", file_bytes=_text_pdf(), async_mode=False,
    )
    doc = Document.objects.get(id=res.document_id)
    assert res.status == DocumentStatus.READY
    assert not [c for c in Chunk.objects.filter(document=doc) if c.meta.get("kind") == "table"]
    assert "tables" not in doc.meta["extraction"]  # pas d'index si aucune table


def test_real_pdf_table_end_to_end(rag_space):
    pytest.importorskip("pdfplumber")
    pytest.importorskip("reportlab")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    buf = BytesIO()
    data = [
        ["Noeud/Cas", "FX [T]", "FY [T]", "FZ [T]", "MX [Tm]", "MY [Tm]", "MZ [Tm]"],
        ["153/ELU+", "-11.4", "-3.1", "-160", "0", "0", "0"],
    ]
    tbl = Table(data)
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(buf, pagesize=A4).build([tbl])

    res = ingest_document_from_upload(
        rag_space_id=rag_space.id, filename="annexe.pdf",
        content_type="application/pdf", file_bytes=buf.getvalue(), async_mode=False,
    )
    assert res.status == DocumentStatus.READY
    doc = Document.objects.get(id=res.document_id)
    tcs = [c for c in Chunk.objects.filter(document=doc) if c.meta.get("kind") == "table"]
    assert tcs, "un chunk-tableau pile_head_forces devrait être créé"
    assert any(r["node"] == "153" and r["fz"] == -160.0 for r in tcs[0].meta["table"]["rows"])


# ----------------------------------------------------- P3-b : tableaux OCR (gated)
def _ocr_words_forces():
    header = [("Noeud/Cas", 10), ("FX", 110), ("[T]", 140), ("FZ", 210), ("MZ", 360)]
    row = [("153/ELU+", 10), ("-11,4", 110), ("-160", 210), ("0", 360)]
    out = []
    for ln, cells in enumerate((header, row), start=1):
        for txt, left in cells:
            out.append({"text": txt, "left": left, "top": ln * 20,
                        "block_num": 1, "par_num": 1, "line_num": ln})
    return out


# Niveau extract_document (ocr_enabled=True en override -> pas d'async forcé) :
# la création de chunk depuis res.tables est déjà couverte par les tests P3-a2.
def _image_only_pdf_with_ocr(monkeypatch, *, words_fn):
    from apps.rag_core.services.ingest import extractors
    from apps.rag_core.services.ingest.extraction import ocr as OCR
    from apps.rag_core.services.ingest.extraction.ocr import OcrResult, OcrPage

    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes", lambda *a, **k: [(1, "")])
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})

    def _fake_ocr_pages(raw, page_numbers, *, languages=None, page_timeout_s=None):
        r = OcrResult(used=True)
        for pn in page_numbers:
            r.pages.append(OcrPage(page_number=pn, text="EFFORTS EN TETES DE PIEUX", confidence=70.0))
        return r

    monkeypatch.setattr(OCR, "ocr_pdf_pages", _fake_ocr_pages)
    monkeypatch.setattr(OCR, "ocr_words_for_pages", words_fn)


def test_ocr_table_reconstruction_gated_and_low_confidence(settings, monkeypatch):
    from apps.rag_core.services.ingest.extraction import extract_document

    _image_only_pdf_with_ocr(monkeypatch, words_fn=lambda raw, pages, **k: {1: _ocr_words_forces()})
    settings.DOCUMENT_TABLE_OCR_ENABLED = True   # P3-b opt-in

    res = extract_document(_text_pdf(), filename="scan.pdf",
                           content_type="application/pdf", ocr_enabled=True)
    assert res.tables, "un tableau OCR devrait être reconstruit"
    meta = res.tables[0]["meta"]
    assert meta["table_type"] == "pile_head_forces"
    assert meta["source"] == "ocr"
    assert meta["confidence"] == "low" and meta["requires_visual_check"] is True
    assert any(r["node"] == "153" and r["fz"] == -160.0 for r in meta["rows"])


def test_ocr_table_disabled_by_default(settings, monkeypatch):
    from apps.rag_core.services.ingest.extraction import extract_document

    called = {"words": False}

    def _spy(*a, **k):
        called["words"] = True
        return {}

    _image_only_pdf_with_ocr(monkeypatch, words_fn=_spy)
    settings.DOCUMENT_TABLE_OCR_ENABLED = False   # défaut : P3-b inactif

    res = extract_document(_text_pdf(), filename="scan.pdf",
                           content_type="application/pdf", ocr_enabled=True)
    assert called["words"] is False               # reconstruction OCR non déclenchée
    assert not res.tables
