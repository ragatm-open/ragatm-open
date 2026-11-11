"""détection, versions, admission, async forcé, file dédiée, codes DWG.

Politique : extension `.dwg` ET en-tête AutoCAD plausible (`ACxxxx` + 5 octets nuls,
≥ 256 octets). Le Content-Type client (`image/vnd.dwg`, `application/acad`…) n'est jamais
décisif ; les signatures fortes (PDF, PNG, ZIP, ELF, PE…) restent prioritaires ; un texte
qui commence par `AC1032` n'est pas un DWG. Versions : AC1015→AC1032 supportées (une par
famille du corpus LibreDWG) ; les autres `ACxxxx` sont reconnues puis refusées
(`dwg_unsupported_version`).
"""
from __future__ import annotations

import importlib

import pytest
from apps.rag_core.services.ingest.extraction import RejectedFile
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction import validation as V
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from django.test import override_settings

from ._builders import make_docx, make_pdf_with_text, make_png
from ._dwg_fixtures import dwg_bytes

DWG_ON = override_settings(DOCUMENT_DWG_ENABLED=True)
DWG_MIME = "application/acad"
SUPPORTED = ("AC1015", "AC1018", "AC1021", "AC1024", "AC1027", "AC1032")
RECOGNIZED_UNSUPPORTED = ("AC1009", "AC1012", "AC1014", "AC1016", "AC1017")


def _sig():
    return importlib.import_module("apps.rag_core.services.ingest.extraction.dwg_signature")


def _ctx(name, ct=""):
    return ExtractionContext(filename=name, declared_content_type=ct)


class TestSignature:
    def test_mime_constant_and_detector_registered(self):
        assert getattr(M, "DWG", None) == DWG_MIME
        assert [n for _, n in M.structured_detectors()] == ["audio_transcript", "dwg", "dxf", "eml", "ifc", "xml"]

    @pytest.mark.parametrize("version", SUPPORTED + RECOGNIZED_UNSUPPORTED)
    def test_dot_dwg_with_plausible_header_is_dwg(self, version):
        assert M.detect_mime(dwg_bytes(version), _ctx("plan.dwg")) == DWG_MIME
        assert _sig().dwg_version(dwg_bytes(version)) == version

    @pytest.mark.parametrize("ct", ["application/acad", "image/vnd.dwg", "application/x-dwg", "drawing/dwg",
                                    "application/octet-stream", "text/plain"])
    def test_declared_content_type_is_ignored(self, ct):
        assert M.detect_mime(dwg_bytes(), _ctx("plan.dwg", ct)) == DWG_MIME
        assert M.detect_mime(b"AC1032 lorem ipsum dolor sit amet " * 20, _ctx("plan.dwg", ct)) == M.PLAIN

    def test_image_vnd_dwg_never_routes_to_the_image_pipeline(self):
        with DWG_ON:
            detected, fmt = V.validate_and_classify(dwg_bytes(), _ctx("plan.dwg", "image/vnd.dwg"))
        assert detected == DWG_MIME and fmt == "dwg"
        assert fmt != V.FMT_IMAGE

    def test_dxf_and_ifc_are_not_dwg(self):
        from ._dxf_builders import build_basic
        from ._ifc_builders import build_header_only

        assert M.detect_mime(build_basic(), _ctx("plan.dxf")) == M.DXF
        assert M.detect_mime(build_header_only("IFC4"), _ctx("m.ifc")) == M.IFC


class TestSecurityPrecedence:
    """Contrôles de priorité des signatures : vrais AVANT le lot, ils doivent le rester."""

    @pytest.mark.parametrize("name,build,expected", [
        ("plan.dxf", lambda: dwg_bytes(), M.OCTET),
        ("plan.bin", lambda: dwg_bytes(), M.OCTET),
        ("plan.dwg", lambda: b"AC1032 lorem ipsum dolor sit amet " * 20, M.PLAIN),
        ("plan.dwg", lambda: b"AC1032" + b"\x00" * 5 + b"\x01" * 40, M.OCTET),  # trop court pour un DWG
        ("plan.dwg", lambda: b"AC9999" + b"\x00" * 600, M.OCTET),  # code de version inconnu
        ("plan.dwg", lambda: bytes((i * 13 + 5) % 256 for i in range(4000)), M.OCTET),
    ], ids=["dxf-ext", "bin-ext", "text-AC1032", "too-short", "unknown-code", "random"])
    def test_no_hijack(self, name, build, expected):
        assert M.detect_mime(build(), _ctx(name)) == expected

    @pytest.mark.parametrize("build,expected", [
        (lambda: b"\x7fELF\x02\x01\x01" + b"\x00" * 600, M.EXECUTABLE),
        (lambda: b"MZ\x90\x00" + b"\x00" * 600, M.EXECUTABLE),
        (make_png, M.PNG), (make_pdf_with_text, M.PDF), (make_docx, M.DOCX),
    ], ids=["elf", "pe", "png", "pdf", "docx-zip"])
    def test_strong_signatures_named_dwg_keep_their_type(self, build, expected):
        assert M.detect_mime(build(), _ctx("plan.dwg")) == expected


class TestVersions:
    @pytest.mark.parametrize("version", SUPPORTED)
    def test_supported_families(self, version):
        sig = _sig()
        assert sig.is_supported_version(version) is True
        assert sig.RELEASE_BY_VERSION[version].startswith("R20") or sig.RELEASE_BY_VERSION[version] == "R2000"

    @pytest.mark.parametrize("version", RECOGNIZED_UNSUPPORTED)
    def test_recognized_but_unsupported_is_a_named_refusal(self, version):
        assert _sig().is_supported_version(version) is False
        with DWG_ON, pytest.raises(RejectedFile) as ei:
            V.validate_and_classify(dwg_bytes(version), _ctx("old.dwg"))
        assert ei.value.code == "dwg_unsupported_version"

    def test_ac1032_release_label(self):
        assert _sig().RELEASE_BY_VERSION["AC1032"] == "R2018"


class TestGate:
    def test_disabled_by_default_is_a_controlled_rejection(self, settings):
        assert settings.DOCUMENT_DWG_ENABLED is False
        with pytest.raises(RejectedFile) as ei:
            V.validate_and_classify(dwg_bytes(), _ctx("plan.dwg"))
        assert ei.value.code == "dwg_ingestion_disabled"

    def test_enabled_classifies_as_dwg(self):
        with DWG_ON:
            assert V.validate_and_classify(dwg_bytes(), _ctx("plan.dwg")) == (DWG_MIME, "dwg")

    def test_admission_does_not_require_the_local_converter(self, settings):
        """Le processus web n'a pas LibreDWG (3A/4) : l'admission repose sur le seul flag."""
        settings.DOCUMENT_DWG_CONVERTER_PATH = "/nonexistent/dwg2dxf"
        with DWG_ON:
            assert V.validate_and_classify(dwg_bytes(), _ctx("plan.dwg"))[1] == "dwg"

    def test_supported_formats_lists_dwg_only_when_enabled(self, settings):
        from apps.rag_core.services.ingest.extraction import supported_formats

        settings.DOCUMENT_DWG_CONVERTER_PATH = "/nonexistent/dwg2dxf"
        with override_settings(DOCUMENT_DWG_ENABLED=False):
            assert "dwg" not in supported_formats()
        with DWG_ON:
            assert "dwg" in supported_formats()


class TestOtherFormatsUnaffected:
    @pytest.mark.parametrize("on", [False, True])
    def test_other_formats_insensitive(self, on):
        with override_settings(DOCUMENT_DWG_ENABLED=on):
            assert V.validate_and_classify(b"Bonjour, ceci est un texte.\n", _ctx("n.txt"))[1] == V.FMT_TXT
            assert V.validate_and_classify(make_pdf_with_text(), _ctx("d.pdf"))[1] == V.FMT_PDF


class TestAsyncAndQueue:
    def test_dwg_forces_async_not_overridable(self, settings):
        from apps.rag_core.services.ingest.async_policy import resolve_async

        with DWG_ON:
            assert resolve_async(explicit=False, format_key="dwg") is True
            assert resolve_async(explicit=None, format_key="dwg") is True
            assert resolve_async(explicit=False, format_key="txt") is False

    def test_ingest_queue_helper_routes_dwg_only_when_enabled(self):
        from apps.rag_core.services.ingest.queues import INGEST_DWG_QUEUE, INGEST_IFC_QUEUE, ingest_queue_for

        assert INGEST_DWG_QUEUE == "ingest_dwg"
        with DWG_ON:
            assert ingest_queue_for(filename="plan.dwg") == INGEST_DWG_QUEUE
            assert ingest_queue_for(filename="PLAN.DWG") == INGEST_DWG_QUEUE
            assert ingest_queue_for(filename="plan.dxf") is None
            assert ingest_queue_for(filename="n.txt") is None
        with override_settings(DOCUMENT_DWG_ENABLED=False):
            assert ingest_queue_for(filename="plan.dwg") is None
        with override_settings(DOCUMENT_IFC_ENABLED=True):
            assert ingest_queue_for(filename="m.ifc") == INGEST_IFC_QUEUE


class TestRetryClassification:
    @pytest.mark.parametrize("code", ["dwg_malformed", "dwg_unsupported_version", "dwg_conversion_failed",
                                      "dwg_conversion_timeout", "dwg_complexity_limit"])
    def test_definitive(self, code):
        from apps.rag_core.services.ingest.retry_strategy import NON_RETRYABLE_ERROR_CODES

        assert code in NON_RETRYABLE_ERROR_CODES

    @pytest.mark.parametrize("code", ["dwg_ingestion_disabled", "dwg_dependency_unavailable"])
    def test_environmental(self, code):
        from apps.rag_core.services.ingest.retry_strategy import ENVIRONMENTAL_ERROR_CODES

        assert code in ENVIRONMENTAL_ERROR_CODES
