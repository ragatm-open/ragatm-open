"""détection, porte, async forcé, file dédiée et codes IFC (`.ifc`).

Politique : extension `.ifc` + en-tête STEP `ISO-10303-21;` (après BOM/blancs). Le
`FILE_SCHEMA` n'est PAS requis pour le routage : un `.ifc` STEP à en-tête invalide va
à l'extracteur (`ifc_malformed` / `ifc_unsupported_schema`), jamais en texte brut. Un
`.step`/`.stp` générique n'est jamais IFC. Content-Type client jamais décisif.
"""
from __future__ import annotations

import pytest
from apps.rag_core.services.ingest.extraction import RejectedFile
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction import validation as V
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from django.test import override_settings

from ._builders import make_docx, make_pdf_with_text, make_png
from ._ifc_builders import build_full, build_header_only

IFC_ON = override_settings(DOCUMENT_IFC_ENABLED=True)
IFC_MIME = "application/x-step"
STEP_AP214 = build_header_only("AUTOMOTIVE_DESIGN")
PLAIN_WITH_ISO = b"Note : le standard ISO-10303-21; decrit les fichiers STEP.\n"
BOM_IFC = b"\xef\xbb\xbf  \n" + build_header_only("IFC4")


def _ctx(name, ct=""):
    return ExtractionContext(filename=name, declared_content_type=ct)


class TestSignature:
    def test_mime_constant_and_detector_registered(self):
        assert getattr(M, "IFC", None) == IFC_MIME
        assert [n for _, n in M.structured_detectors()] == ["audio_transcript", "dwg", "dxf", "eml", "ifc", "xml"]

    @pytest.mark.parametrize("build", [lambda: build_full("IFC4"), lambda: build_header_only("IFC2X3"),
                                       lambda: build_header_only("IFC4X3"), lambda: BOM_IFC, lambda: STEP_AP214],
                             ids=["ifc4-full", "ifc2x3-header", "ifc4x3-header", "bom", "non-ifc-schema-routed"])
    def test_dot_ifc_with_step_header_is_ifc(self, build):
        assert M.detect_mime(build(), _ctx("model.ifc")) == IFC_MIME

    @pytest.mark.parametrize("ct", ["application/x-step", "model/step", "application/octet-stream", "text/plain"])
    def test_declared_content_type_is_ignored(self, ct):
        assert M.detect_mime(build_header_only("IFC4"), _ctx("model.ifc", ct)) == IFC_MIME
        assert M.detect_mime(PLAIN_WITH_ISO, _ctx("model.ifc", ct)) == M.PLAIN

    @pytest.mark.parametrize("name,build,expected", [
        ("part.step", lambda: build_header_only("IFC4"), M.PLAIN),
        ("part.stp", lambda: STEP_AP214, M.PLAIN),
        ("model.txt", lambda: build_header_only("IFC4"), M.PLAIN),
        ("model.ifcxml", lambda: b'<?xml version="1.0"?><ifcXML/>', M.PLAIN),
        ("model.ifc", lambda: PLAIN_WITH_ISO, M.PLAIN),
    ], ids=["step", "stp", "txt", "ifcxml", "plain-mentions-iso"])
    def test_no_hijack(self, name, build, expected):
        assert M.detect_mime(build(), _ctx(name)) == expected

    @pytest.mark.parametrize("build,expected", [
        (lambda: b"\x7fELF\x02\x01\x01" + b"\x00" * 50, M.EXECUTABLE),
        (lambda: b"MZ\x90\x00" + b"\x00" * 50, M.EXECUTABLE),
        (make_png, M.PNG), (make_pdf_with_text, M.PDF), (make_docx, M.DOCX),
        (lambda: b"<!DOCTYPE html><html><body>hi</body></html>", M.HTML),
        (lambda: b"a,b,c\n1,2,3\n4,5,6\n", M.CSV),
        (lambda: b"PK\x03\x04" + b"\x00" * 40, M.ZIP),
    ], ids=["elf", "pe", "png", "pdf", "docx", "html", "csv", "zip-ifczip"])
    def test_strong_and_text_signatures_named_ifc_keep_their_type(self, build, expected):
        assert M.detect_mime(build(), _ctx("renamed.ifc")) == expected

    def test_xml_and_dxf_are_not_ifc(self):
        assert M.detect_mime(b'<?xml version="1.0"?><a/>', _ctx("a.xml")) == M.XML
        assert M.detect_mime(b"  0\nSECTION\n  2\nHEADER\n", _ctx("a.dxf")) == M.DXF


class TestGate:
    def test_disabled_by_default(self, settings):
        assert getattr(settings, "DOCUMENT_IFC_ENABLED", False) is False
        with pytest.raises(RejectedFile) as ei:
            V.validate_and_classify(build_header_only("IFC4"), _ctx("m.ifc"))
        assert ei.value.code == "ifc_ingestion_disabled" and "IFC" in ei.value.detail

    @IFC_ON
    def test_enabled_classifies_as_ifc(self):
        detected, fmt = V.validate_and_classify(build_header_only("IFC4"), _ctx("m.ifc"))
        assert detected == IFC_MIME and fmt == getattr(V, "FMT_IFC", None) == "ifc"

    @IFC_ON
    def test_admission_does_not_require_the_local_parser(self, monkeypatch):
        """le web admet sur le flag seul (image générale sans `[ifc]`) ;
        la dépendance est contrôlée côté worker, par l'extracteur."""
        from apps.rag_core.services.ingest.extraction import ifc_extractor as X
        from apps.rag_core.services.ingest.extraction import supported_formats

        monkeypatch.setattr(X, "parser_available", lambda: False)
        detected, fmt = V.validate_and_classify(build_header_only("IFC4"), _ctx("m.ifc"))
        assert fmt == "ifc" and "ifc" in supported_formats()
        with pytest.raises(RejectedFile) as ei:
            X.IfcExtractor().extract(build_header_only("IFC4"), _ctx("m.ifc"))
        assert ei.value.code == "ifc_dependency_unavailable"

    @pytest.mark.parametrize("on", [False, True])
    def test_other_formats_insensitive(self, on):
        with override_settings(DOCUMENT_IFC_ENABLED=on, DOCUMENT_XML_ENABLED=True, DOCUMENT_DXF_ENABLED=True):
            assert V.validate_and_classify(b"hello plain", _ctx("a.txt"))[1] == V.FMT_TXT
            assert V.validate_and_classify(b'<?xml version="1.0"?><a>x</a>', _ctx("a.xml"))[1] == V.FMT_XML
            assert V.validate_and_classify(b"  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n  0\nEOF\n", _ctx("a.dxf"))[1] == V.FMT_DXF

    @IFC_ON
    def test_supported_formats_lists_ifc_only_when_enabled(self):
        from apps.rag_core.services.ingest.extraction import supported_formats

        assert "ifc" in supported_formats()
        with override_settings(DOCUMENT_IFC_ENABLED=False):
            assert "ifc" not in supported_formats()


class TestAsyncAndQueue:
    def test_ifc_forces_async_not_overridable(self, settings):
        from apps.rag_core.services.ingest import async_policy as AP

        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        fn = getattr(AP, "ifc_forces_async", None)
        assert fn is not None
        assert fn("ifc") is True and fn(None) is False and fn("dxf") is False
        assert AP.resolve_async(explicit="false", format_key="ifc") is True

    @IFC_ON
    def test_ingest_queue_helper_routes_ifc_only(self):
        from apps.rag_core.services.ingest import queues as Q

        assert Q.INGEST_IFC_QUEUE == "ingest_ifc"
        assert Q.ingest_queue_for(filename="model.ifc") == "ingest_ifc"
        assert Q.ingest_queue_for(filename="MODEL.IFC") == "ingest_ifc"
        for name in ("plan.dxf", "data.xml", "doc.pdf", "", None):
            assert Q.ingest_queue_for(filename=name) is None
        with override_settings(DOCUMENT_IFC_ENABLED=False):
            assert Q.ingest_queue_for(filename="model.ifc") is None


class TestRetryability:
    @pytest.mark.parametrize("code", ["ifc_malformed", "ifc_unsupported_schema", "ifc_complexity_limit"])
    def test_definitive(self, code):
        from apps.rag_core.services.ingest import retry_strategy as rs

        assert code in rs.NON_RETRYABLE_ERROR_CODES

    @pytest.mark.parametrize("code", ["ifc_ingestion_disabled", "ifc_dependency_unavailable"])
    def test_environmental(self, code):
        from apps.rag_core.services.ingest import retry_strategy as rs

        assert code in rs.ENVIRONMENTAL_ERROR_CODES
