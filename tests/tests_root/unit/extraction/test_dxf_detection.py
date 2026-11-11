"""détection, porte, async et codes du format DXF (`.dxf`).

Politique : extension `.dxf` + signature (magic binaire « AutoCAD Binary DXF » ou
structure ASCII `0/SECTION` puis `2/<HEADER|…>`), jamais le seul mot SECTION, jamais le
Content-Type client. Hook : après les signatures fortes, avant le
repli texte. Symboles nouveaux résolus paresseusement (ligne de base collectable).
"""
from __future__ import annotations

import pytest
from apps.rag_core.services.ingest.extraction import RejectedFile
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction import validation as V
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from django.test import override_settings

from ._builders import make_docx, make_pdf_with_text, make_png
from ._dxf_builders import build_basic, build_binary

DXF_ON = override_settings(DOCUMENT_DXF_ENABLED=True)
DXF_MIME = "application/dxf"
# `<TITLE>` et non `<HEADER>` : l'heuristique HTML existante prend `<head…` pour du HTML.
XML_DOC = b'<?xml version="1.0"?><SECTION><TITLE>x</TITLE></SECTION>'
PLAIN_WITH_SECTION = b"Compte rendu\nSECTION 2 : HEADER\n0\nrien a voir\n"
ASCII_MIN = b"  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n  0\nEOF\n"
ASCII_COMMENT_FIRST = b"999\ncommentaire\r\n  0\r\nSECTION\r\n  2\r\nENTITIES\r\n  0\r\nENDSEC\r\n  0\r\nEOF\r\n"


def _ctx(name, ct=""):
    return ExtractionContext(filename=name, declared_content_type=ct)


class TestSignature:
    def test_mime_constant_and_detector_registered(self):
        assert getattr(M, "DXF", None) == DXF_MIME
        names = [n for _, n in M.structured_detectors()]
        assert "dxf" in names and "xml" in names

    @pytest.mark.parametrize("build", [build_basic, build_binary, lambda: ASCII_MIN, lambda: ASCII_COMMENT_FIRST],
                             ids=["ascii-ezdxf", "binary", "ascii-min", "ascii-comment-crlf"])
    def test_dot_dxf_with_signature_is_dxf(self, build):
        assert M.detect_mime(build(), _ctx("plan.dxf")) == DXF_MIME

    @pytest.mark.parametrize("ct", ["image/vnd.dxf", "application/dxf", "application/octet-stream", "text/plain"])
    def test_declared_content_type_is_ignored(self, ct):
        assert M.detect_mime(build_basic(), _ctx("plan.dxf", ct)) == DXF_MIME
        assert M.detect_mime(PLAIN_WITH_SECTION, _ctx("plan.dxf", ct)) == M.PLAIN

    def test_image_vnd_dxf_never_routes_to_image_pipeline(self):
        detected = M.detect_mime(build_basic(), _ctx("plan.dxf", "image/vnd.dxf"))
        assert detected == DXF_MIME and detected not in M.IMAGE_MIMES

    @pytest.mark.parametrize("name,build,expected", [
        ("plan.txt", build_basic, M.PLAIN),
        ("plan.md", build_basic, M.MARKDOWN),
        ("plan.dwg", build_basic, M.PLAIN),
        ("plan.dxf", lambda: PLAIN_WITH_SECTION, M.PLAIN),
        ("data.xml", lambda: XML_DOC, M.XML),
        ("data.dxf", lambda: XML_DOC, M.PLAIN),
    ], ids=["txt", "md", "dwg-ext", "plain-with-section", "xml-stays-xml", "xml-named-dxf"])
    def test_no_hijack(self, name, build, expected):
        assert M.detect_mime(build(), _ctx(name)) == expected

    @pytest.mark.parametrize("build,expected", [
        (lambda: b"\x7fELF\x02\x01\x01" + b"\x00" * 50, M.EXECUTABLE),
        (lambda: b"MZ\x90\x00" + b"\x00" * 50, M.EXECUTABLE),
        (make_png, M.PNG),
        (make_pdf_with_text, M.PDF),
        (make_docx, M.DOCX),
        (lambda: b"<!DOCTYPE html><html><body>hi</body></html>", M.HTML),
        (lambda: b"a,b,c\n1,2,3\n4,5,6\n", M.CSV),
    ], ids=["elf", "pe", "png", "pdf", "docx", "html", "csv"])
    def test_strong_and_text_signatures_named_dxf_keep_their_type(self, build, expected):
        assert M.detect_mime(build(), _ctx("renamed.dxf")) == expected

    def test_random_binary_named_dxf_is_octet(self):
        assert M.detect_mime(bytes(range(256)) * 4, _ctx("x.dxf")) == M.OCTET

    def test_malformed_but_signed_is_dxf(self):
        assert M.detect_mime(b"  0\nSECTION\n  2\nENTITIES\n  0\nLINE\n  8\n", _ctx("bad.dxf")) == DXF_MIME


class TestGate:
    def test_disabled_by_default_is_a_controlled_rejection(self, settings):
        assert getattr(settings, "DOCUMENT_DXF_ENABLED", False) is False
        with pytest.raises(RejectedFile) as ei:
            V.validate_and_classify(build_basic(), _ctx("plan.dxf"))
        assert ei.value.code == "dxf_ingestion_disabled"
        assert "DXF" in ei.value.detail

    @DXF_ON
    def test_enabled_classifies_as_dxf(self):
        detected, fmt = V.validate_and_classify(build_basic(), _ctx("plan.dxf"))
        assert detected == DXF_MIME
        assert fmt == getattr(V, "FMT_DXF", None) == "dxf"

    @DXF_ON
    def test_dependency_missing_is_controlled(self, monkeypatch):
        monkeypatch.setattr(V, "dxf_parser_available", lambda: False)
        with pytest.raises(RejectedFile) as ei:
            V.validate_and_classify(build_basic(), _ctx("plan.dxf"))
        assert ei.value.code == "dxf_dependency_unavailable"

    @pytest.mark.parametrize("on", [False, True])
    def test_other_formats_insensitive_to_flag(self, on):
        with override_settings(DOCUMENT_DXF_ENABLED=on, DOCUMENT_XML_ENABLED=True):
            assert V.validate_and_classify(b"hello plain", _ctx("a.txt"))[1] == V.FMT_TXT
            assert V.validate_and_classify(XML_DOC, _ctx("a.xml"))[1] == V.FMT_XML
            assert V.validate_and_classify(make_docx(), _ctx("a.docx"))[1] == V.FMT_DOCX

    @DXF_ON
    def test_supported_formats_lists_dxf_only_when_enabled(self):
        from apps.rag_core.services.ingest.extraction import supported_formats

        assert "dxf" in supported_formats()
        with override_settings(DOCUMENT_DXF_ENABLED=False):
            assert "dxf" not in supported_formats()


class TestAsyncPolicy:
    def test_dxf_forces_async(self, settings):
        from apps.rag_core.services.ingest import async_policy as AP

        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        fn = getattr(AP, "dxf_forces_async", None)
        assert fn is not None, "async_policy.dxf_forces_async absent"
        assert fn("dxf") is True
        assert fn(None) is False
        for fmt in ("pdf", "txt", "xml", "pptx", "excel"):
            assert fn(fmt) is False
        assert AP.resolve_async(explicit="false", format_key="dxf") is True
        assert AP.resolve_async(explicit="false", format_key="xml") is False


class TestRetryability:
    @pytest.mark.parametrize("code", ["dxf_malformed", "dxf_unsupported_version", "dxf_complexity_limit"])
    def test_definitive(self, code):
        from apps.rag_core.services.ingest import retry_strategy as rs

        assert code in rs.NON_RETRYABLE_ERROR_CODES

    @pytest.mark.parametrize("code", ["dxf_ingestion_disabled", "dxf_dependency_unavailable"])
    def test_environmental(self, code):
        from apps.rag_core.services.ingest import retry_strategy as rs

        assert code in rs.ENVIRONMENTAL_ERROR_CODES
