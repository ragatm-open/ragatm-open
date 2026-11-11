# tests/tests_root/rag_core/test_entities_ingestion.py
"""Tests P2 — intégration : les entités métier se propagent aux Chunk.meta et
sont agrégées dans Document.meta["extraction"] lors de l'ingestion."""
from __future__ import annotations

from io import BytesIO

import pytest

from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest import ingest_document_from_upload

pytestmark = pytest.mark.django_db


def _plan_pdf() -> bytes:
    """PDF texte (natif) simulant un plan : entités lisibles sans OCR."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    # page 1 : plan M01
    for line in ["VUE EN PLAN MASSIF M01", "NAPPE INFERIEURE COUPE A-A", "8 HA20 e=0.15 Ø720 platine 400x400"]:
        c.drawString(72, 800 - 20 * ["VUE EN PLAN MASSIF M01", "NAPPE INFERIEURE COUPE A-A", "8 HA20 e=0.15 Ø720 platine 400x400"].index(line), line)
    c.showPage()
    # page 2 : nomenclature
    c.drawString(72, 800, "NOMENCLATURE GENERALE DES ACIERS")
    c.drawString(72, 780, "Repere 12 HA14 longueur 660 MASSIF M02")
    c.showPage()
    c.save()
    return buf.getvalue()


def test_ingestion_attaches_entities_to_chunks_and_aggregates(rag_space):
    res = ingest_document_from_upload(
        rag_space_id=rag_space.id,
        filename="plan.pdf",
        content_type="application/pdf",
        file_bytes=_plan_pdf(),
        async_mode=False,
    )
    assert res.status == DocumentStatus.READY

    doc = Document.objects.get(id=res.document_id)
    chunks = list(Chunk.objects.filter(document=doc).order_by("chunk_index"))
    assert chunks

    # Chaque chunk porte page_type + entities.
    by_page = {c.page_number: c.meta for c in chunks}
    assert by_page[1]["page_type"] == "plan"
    assert "M01" in by_page[1]["entities"]["massifs"]
    assert by_page[2]["page_type"] == "nomenclature"

    # Agrégation document-level.
    ext = doc.meta["extraction"]
    assert set(ext["detected_massifs"]) >= {"M01", "M02"}
    assert "vue_en_plan" in ext["detected_views"]
    assert ext["pages_by_type"].get("plan", 0) >= 1
    assert ext["pages_by_type"].get("nomenclature", 0) >= 1
