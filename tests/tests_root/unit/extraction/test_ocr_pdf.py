"""Tests extracteur PDF + OCR optionnel.

L'OCR réel (tesseract) n'est pas requis : on injecte un provider stub via
monkeypatch sur `ocr.get_provider`, ce qui permet de tester le DÉCLENCHEMENT,
les limites et les timeouts sans dépendance binaire.
"""
import pytest

from apps.rag_core.services.ingest.extraction import ExtractionError, extract_document
from apps.rag_core.services.ingest.extraction import ocr as ocr_mod

from ._builders import make_pdf_image_only, make_pdf_with_text, make_png


class _StubImage:
    def save(self, buf, format="PNG"):
        buf.write(b"\x89PNG\r\n\x1a\n stub")


class _StubProvider:
    """Provider OCR factice : renvoie un texte déterministe et une confiance."""

    name = "stub"
    external = False

    # Texte >= seuil d'acceptation par page (DOCUMENT_OCR_MIN_CHARS_PER_PAGE=24).
    def __init__(self, *, text="TEXTE OCR LISIBLE EXTRAIT DU SCAN", conf=87.0, raise_raster=False):
        from tests_root.process_witness import TemoinPartage

        self.text = text
        self.conf = conf
        self.raise_raster = raise_raster
        # L'extraction s'exécute dans un processus enfant (HARDENING-03) : un entier
        # incrémenté ici resterait invisible du test. Le témoin franchit la frontière.
        self.appels = TemoinPartage()

    @property
    def calls(self) -> int:
        return len(self.appels)

    def ocr_image_bytes(self, raw, *, languages, budget_s=None):
        self.appels.note(budget_s)
        # Contrat provider : (texte, confiance, rotation_appliquée).
        return self.text, self.conf, 0

    def rasterize_pdf(self, raw, page_indices):
        if self.raise_raster:
            raise ocr_mod.OcrUnavailable("no raster", code="ocr_pdf_rasterizer_missing")
        return [_StubImage() for _ in page_indices]


@pytest.fixture
def stub_ocr(monkeypatch):
    provider = _StubProvider()
    monkeypatch.setattr(ocr_mod, "get_provider", lambda name=None: provider)
    return provider


class TestPdfNative:
    def test_text_pdf_does_not_trigger_ocr(self, stub_ocr, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        r = extract_document(make_pdf_with_text("Contenu lisible nativement."),
                             filename="t.pdf")
        assert r.metadata["ocr_used"] is False
        assert stub_ocr.calls == 0
        assert "lisible" in r.text
        # method = "pdf:<moteur natif>" depuis le fallback multi-moteur (P0).
        assert r.method.startswith("pdf:")
        assert not r.method.endswith("+ocr")


class TestPdfOcrDisabled:
    def test_image_only_pdf_clear_error_when_ocr_off(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        with pytest.raises(ExtractionError) as ei:
            extract_document(make_pdf_image_only(), filename="scan.pdf")
        assert ei.value.code == "pdf_scanned_requires_ocr"


class TestPdfOcrEnabled:
    def test_image_only_pdf_triggers_ocr(self, stub_ocr, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        r = extract_document(make_pdf_image_only(), filename="scan.pdf")
        assert r.metadata["ocr_used"] is True
        # Contrat de PROPAGATION (mode confiance ON) : `stub_ocr` remplace le provider,
        # donc la valeur vient du stub. Le mode OFF est couvert par
        # tests/tests_root/rag_core/test_ocr_optional_confidence.py.
        assert r.metadata.get("ocr_confidence") == 87.0
        assert "TEXTE OCR" in r.text
        # method = "pdf:<moteur>+ocr" (P0 fallback + OCR).
        assert r.method.startswith("pdf:") and r.method.endswith("+ocr")
        assert stub_ocr.calls >= 1

    def test_ocr_unavailable_is_graceful(self, monkeypatch, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        provider = _StubProvider(raise_raster=True)
        monkeypatch.setattr(ocr_mod, "get_provider", lambda name=None: provider)
        # aucune page texte + rastériseur OCR indisponible -> erreur claire et
        # SPÉCIFIQUE (moteur absent ≠ document sans texte), pas de crash brutal.
        with pytest.raises(ExtractionError) as ei:
            extract_document(make_pdf_image_only(), filename="scan.pdf")
        assert ei.value.code == "ocr_engine_missing"

    def test_ocr_max_pages_limit(self, stub_ocr, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        settings.DOCUMENT_OCR_MAX_PAGES = 0
        res = ocr_mod.ocr_pdf_pages(make_pdf_image_only(), [1, 2, 3])
        assert res.partial is True
        assert "ocr_max_pages_reached" in res.warnings


class TestImageExtractor:
    def test_image_ocr(self, stub_ocr, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        r = extract_document(make_png(), filename="photo.png")
        assert r.metadata["ocr_used"] is True
        assert "TEXTE OCR" in r.text


class TestOcrProviderResolution:
    def test_unknown_provider_no_external_call(self, settings):
        # un provider inconnu lève AVANT toute I/O (garantie : pas d'appel externe)
        settings.DOCUMENT_OCR_PROVIDER = "some-cloud-api"
        with pytest.raises(ocr_mod.OcrUnavailable) as ei:
            ocr_mod.get_provider()
        assert ei.value.code == "ocr_provider_not_implemented"

    def test_disabled_provider(self, settings):
        settings.DOCUMENT_OCR_PROVIDER = "none"
        with pytest.raises(ocr_mod.OcrDisabled):
            ocr_mod.get_provider()
