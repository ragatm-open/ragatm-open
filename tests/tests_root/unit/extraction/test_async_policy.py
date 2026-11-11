"""Décision produit : OCR activé => ingestion forcément asynchrone."""
from apps.rag_core.services.ingest.extraction import config as xcfg
from apps.rag_core.services.ingest.pipeline import _async_ingest_enabled


class TestOcrForcesAsync:
    def test_ocr_disabled_does_not_force(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        assert xcfg.ocr_forces_async() is False

    def test_ocr_enabled_forces_async_flag(self, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        assert xcfg.ocr_forces_async() is True

    def test_pipeline_async_forced_when_ocr_on(self, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        settings.RAG_INGEST_ASYNC_DEFAULT = False  # même si on demande sync
        assert _async_ingest_enabled() is True

    def test_pipeline_respects_default_when_ocr_off(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert _async_ingest_enabled() is False
