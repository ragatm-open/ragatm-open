"""Tests Lot 1 — socle d'ingestion image : ouverture sûre + extracteur.

L'OCR réel n'est pas requis : provider stub via monkeypatch sur `ocr.get_provider`.
Les cas sécurité (octets/pixels/corruption/bombe) testent `safe_open_image`
directement (déterministe, sans OCR).
"""
import io

import pytest

from apps.rag_core.services.ingest.extraction import ExtractionError, extract_document
from apps.rag_core.services.ingest.extraction import ocr as ocr_mod
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.image_extractor import safe_open_image
from apps.rag_core.services.ingest.extraction.validation import validate_and_classify

from ._builders import make_png


def _jpeg(width=12, height=10, color=(200, 120, 40)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


def _ctx(name="f", ct="", ocr=None):
    return ExtractionContext(filename=name, declared_content_type=ct, ocr_enabled=ocr)


class _StubProvider:
    name = "stub"
    external = False

    def __init__(self, *, text="TEXTE OCR IMAGE", conf=88.0):
        from tests_root.process_witness import TemoinPartage

        self.text, self.conf = text, conf
        # L'extraction s'exécute dans un processus enfant (HARDENING-03) : un entier
        # incrémenté ici resterait invisible du test. Le témoin franchit la frontière.
        self.appels = TemoinPartage()

    @property
    def calls(self) -> int:
        return len(self.appels)

    def ocr_image_bytes(self, raw, *, languages, budget_s=None):
        self.appels.note(budget_s)
        return self.text, self.conf, 0


@pytest.fixture
def stub_ocr(monkeypatch):
    p = _StubProvider()
    monkeypatch.setattr(ocr_mod, "get_provider", lambda name=None: p)
    return p


# --------------------------------------------------------------- safe_open_image
class TestSafeOpenImage:
    def test_valid_png_ok(self):
        img, info = safe_open_image(make_png(8, 8), max_bytes=10_000_000, max_pixels=40_000_000)
        assert info["format"] == "PNG"
        assert (info["width"], info["height"]) == (8, 8)
        assert info["mode"] in ("RGB", "RGBA", "L")
        assert info["exif_orientation_applied"] is False

    def test_valid_jpeg_ok(self):
        img, info = safe_open_image(_jpeg(12, 10), max_bytes=10_000_000, max_pixels=40_000_000)
        assert info["format"] == "JPEG"
        assert (info["width"], info["height"]) == (12, 10)

    def test_corrupted_image(self):
        truncated = make_png(16, 16)[:24]  # signature PNG + corps tronqué
        with pytest.raises(ExtractionError) as ei:
            safe_open_image(truncated, max_bytes=10_000_000, max_pixels=40_000_000)
        assert ei.value.code == "image_corrupted"

    def test_not_an_image(self):
        with pytest.raises(ExtractionError) as ei:
            safe_open_image(b"ceci n'est pas une image", max_bytes=10_000_000, max_pixels=40_000_000)
        assert ei.value.code == "image_corrupted"

    def test_too_many_pixels(self):
        # image 8x8 = 64 px, limite 10 px -> refus AVANT OCR
        with pytest.raises(ExtractionError) as ei:
            safe_open_image(make_png(8, 8), max_bytes=10_000_000, max_pixels=10)
        assert ei.value.code == "image_too_large"

    def test_too_many_bytes(self):
        with pytest.raises(ExtractionError) as ei:
            safe_open_image(make_png(8, 8), max_bytes=10, max_pixels=40_000_000)
        assert ei.value.code == "image_too_large"

    def test_decompression_bomb_mapped(self, monkeypatch):
        from PIL import Image

        def _boom(*a, **k):
            raise Image.DecompressionBombError("bomb")

        monkeypatch.setattr(Image, "open", _boom)
        with pytest.raises(ExtractionError) as ei:
            safe_open_image(make_png(8, 8), max_bytes=10_000_000, max_pixels=40_000_000)
        assert ei.value.code == "image_too_large"


# --------------------------------------------------------------- extracteur image
class TestImageExtractor:
    def test_png_ocr_enriched_meta(self, stub_ocr, settings):
        # Lot 2 : l'OCR image est INDÉPENDANT de l'OCR PDF global -> OCRisée même off.
        settings.DOCUMENT_OCR_ENABLED = False
        settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
        r = extract_document(make_png(20, 16), filename="photo.png")
        assert r.metadata["ocr_used"] is True
        assert r.metadata["type"] == "image"
        assert r.method == "image:ocr"
        assert r.metadata["requires_visual_check"] is True
        assert r.metadata["image"]["width"] == 20 and r.metadata["image"]["height"] == 16
        assert r.metadata["image"]["format"] == "PNG"
        assert "TEXTE OCR IMAGE" in r.text
        assert stub_ocr.calls >= 1

    def test_jpeg_ocr(self, stub_ocr, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        r = extract_document(_jpeg(30, 20), filename="scan.jpg")
        assert r.metadata["type"] == "image"
        assert "TEXTE OCR IMAGE" in r.text


# --------------------------------------------------------------- porte image (Lot 2)
class TestGate:
    def test_image_accepted_regardless_of_global_ocr(self, settings):
        settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
        _, fmt = validate_and_classify(make_png(), _ctx("a.png", ocr=False))
        assert fmt == "image"

    def test_image_rejected_when_ingest_disabled(self, settings):
        settings.DOCUMENT_IMAGE_INGEST_ENABLED = False
        with pytest.raises(ExtractionError) as ei:
            validate_and_classify(make_png(), _ctx("a.png"))
        assert ei.value.code == "image_ingest_disabled"
