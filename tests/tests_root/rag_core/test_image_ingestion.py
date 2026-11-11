# tests/tests_root/rag_core/test_image_ingestion.py
"""Tests Lot 2 — ingestion image bout-en-bout (OCR mocké) + isolation RagSpace.

Vérifie qu'une image uploadée est OCRisée AUTOMATIQUEMENT (indépendamment de
DOCUMENT_OCR_ENABLED), indexée (chunks), avec métadonnées image, et que les
chunks restent isolés dans leur RagSpace.
"""
from __future__ import annotations

import io

import pytest

from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest import ingest_document_from_upload
from apps.rag_core.services.ingest.extraction import ocr as ocr_mod
from apps.rag_core.services.ingest.extraction.base import RejectedFile

pytestmark = pytest.mark.django_db


def _png(width=24, height=18) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


class _StubProvider:
    name = "stub"
    external = False

    def __init__(self, text="GRUE G4 COFFRAGE ARMATURES HA12 HA8 NOMENCLATURE"):
        self.text = text

    def ocr_image_bytes(self, raw, *, languages, budget_s=None):
        # `budget_s` : budget de page TOTAL (OCR-TIMEOUT-HARDENING-02).
        return self.text, 90.0, 0


@pytest.fixture
def stub_ocr(monkeypatch):
    monkeypatch.setattr(ocr_mod, "get_provider", lambda name=None: _StubProvider())


def test_image_upload_auto_ocr_ready(rag_space, settings, stub_ocr):
    # OCR global DÉSACTIVÉ : l'image doit quand même être OCRisée (découplage Lot 2).
    settings.DOCUMENT_OCR_ENABLED = False
    settings.DOCUMENT_IMAGE_INGEST_ENABLED = True

    res = ingest_document_from_upload(
        rag_space_id=rag_space.id,
        filename="plan.png",
        content_type="image/png",
        file_bytes=_png(),
        async_mode=False,
    )
    assert res.status == DocumentStatus.READY
    assert res.chunks_created >= 1

    doc = Document.objects.get(id=res.document_id)
    ext = doc.meta["extraction"]
    assert ext["type"] == "image"
    assert ext["method"] == "image:ocr"
    assert ext["ocr_used"] is True
    assert ext["image"]["format"] == "PNG"
    assert ext["image"]["width"] == 24 and ext["image"]["height"] == 18
    # texte OCR présent dans les chunks (recherche possible)
    chunks = list(Chunk.objects.filter(document=doc))
    assert chunks and chunks[0].page_number == 1
    assert "GRUE G4" in " ".join(c.text for c in chunks)


def test_image_ingest_disabled_fails(rag_space, settings, stub_ocr):
    """INGEST-1E2 : porte image fermée => refus par la classification canonique
    AVANT toute création (même code que l'upload HTTP), aucun Document."""
    settings.DOCUMENT_IMAGE_INGEST_ENABLED = False
    with pytest.raises(RejectedFile) as exc:
        ingest_document_from_upload(
            rag_space_id=rag_space.id,
            filename="plan.png",
            content_type="image/png",
            file_bytes=_png(),
            async_mode=False,
        )
    assert exc.value.code == "image_ingest_disabled"
    assert Document.objects.filter(rag_space=rag_space).count() == 0


def test_image_chunks_isolated_per_ragspace(rag_space, workspace, settings, stub_ocr):
    from apps.rag_core.models import RagSpace

    settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
    other = RagSpace.objects.create(name="S-other", workspace=workspace)

    res = ingest_document_from_upload(
        rag_space_id=rag_space.id, filename="p.png",
        content_type="image/png", file_bytes=_png(), async_mode=False,
    )
    doc = Document.objects.get(id=res.document_id)

    # Les chunks appartiennent au bon RagSpace et sont invisibles de l'autre.
    assert Chunk.objects.filter(document=doc, rag_space=rag_space).count() >= 1
    assert Chunk.objects.filter(rag_space=other).count() == 0
