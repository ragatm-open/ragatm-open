"""Décision sync/async **centralisée** (ingest.async_policy).

Priorité : OCR / Excel / PPTX (forcés) > override explicite (body > query > header) >
RAG_INGEST_ASYNC_DEFAULT.
"""
import pytest

from apps.rag_core.services.ingest.async_policy import (
    pptx_forces_async,
    resolve_async,
    should_async_from_request,
)


class _Req:
    """Faux request DRF : query_params + headers dict-like suffisent."""

    def __init__(self, query=None, headers=None):
        self.query_params = query or {}
        self.headers = headers or {}


class TestResolveAsync:
    @pytest.mark.parametrize(
        ("ocr", "default", "explicit", "expected"),
        [
            (False, False, None, False),
            (False, True, None, True),
            (False, False, "true", True),
            (False, True, "false", False),
            (True, False, "false", True),
            (False, False, "banana", False),
        ],
        ids=[
            "default-off",
            "default-on",
            "explicit-true-overrides-default-off",
            "explicit-false-overrides-default-on",
            "ocr-forces-async-over-explicit-false",
            "invalid-value-is-falsey",
        ],
    )
    def test_priority_ocr_then_explicit_then_default(self, settings, ocr, default, explicit, expected):
        settings.DOCUMENT_OCR_ENABLED = ocr
        settings.RAG_INGEST_ASYNC_DEFAULT = default
        assert resolve_async(explicit=explicit) is expected


class TestPptxForcesAsync:
    """Lot P1 — une présentation est TOUJOURS ingérée en asynchrone.

    Même motif qu'Excel : archive compressée, nombre de formes imprévisible. On ne met
    jamais ça dans le cycle d'une requête HTTP.
    """

    def test_pptx_format_forces_async(self):
        assert pptx_forces_async("pptx") is True

    def test_other_formats_do_not(self):
        for fmt in ("pdf", "docx", "txt", "csv", "html", "markdown", "image"):
            assert pptx_forces_async(fmt) is False

    def test_no_format_key_does_not_force(self):
        """Les chemins qui ignorent le format (reindex) ne doivent pas être impactés."""
        assert pptx_forces_async(None) is False

    def test_pptx_wins_over_explicit_false(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert resolve_async(explicit="false", format_key="pptx") is True

    def test_pptx_wins_over_default_off(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert resolve_async(explicit=None, format_key="pptx") is True

    def test_non_pptx_still_respects_the_default(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert resolve_async(explicit=None, format_key="docx") is False

    def test_request_path_forces_async_for_pptx(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = _Req(query={"async": "false"})
        assert should_async_from_request(req, format_key="pptx") is True


class TestShouldAsyncFromRequest:
    def test_query_param(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert should_async_from_request(_Req(query={"async": "true"})) is True

    def test_header(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert should_async_from_request(_Req(headers={"x-rag-async": "true"})) is True

    def test_body_value_takes_precedence(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        # query dit false, body dit true => body gagne.
        req = _Req(query={"async": "false"})
        assert should_async_from_request(req, body_value=True) is True

    def test_query_over_header(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = _Req(query={"async": "false"}, headers={"x-rag-async": "true"})
        assert should_async_from_request(req) is False

    def test_falls_back_to_setting(self, settings):
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = True
        assert should_async_from_request(_Req()) is True

    def test_ocr_forces_async(self, settings):
        settings.DOCUMENT_OCR_ENABLED = True
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = _Req(query={"async": "false"})
        assert should_async_from_request(req) is True
