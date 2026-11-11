"""détection et porte du format XML (`.xml`).

Politique : extension `.xml` compatible + signature structurelle (déclaration `<?xml`,
`<!DOCTYPE` non-HTML, ou balise racine bien formée après BOM/blancs/commentaires) ;
la validation sécurisée elle-même vit dans l'extracteur. Le detector passe par le hook
après les signatures fortes, avant le repli texte.

Aucun fichier `.html`, `.svg`, `.md`, `.txt`, OOXML ou exécutable ne doit être « volé ».
Les symboles introduits par ce lot sont résolus paresseusement (ligne de base
collectable).
"""
from __future__ import annotations

import pytest
from apps.rag_core.services.ingest.extraction import RejectedFile
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction import validation as V
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from django.test import override_settings

from ._builders import make_docx, make_pdf_with_text, make_png

XML_ON = override_settings(DOCUMENT_XML_ENABLED=True)
XML_MIME = "application/xml"

BASIC = b'<?xml version="1.0" encoding="UTF-8"?>\n<project><name>H\xc3\xb4pital</name></project>\n'
NO_DECL = b"<project><name>Hopital</name></project>"
BOM_NO_DECL = b"\xef\xbb\xbf  \n<!-- en-t\xc3\xaate -->\n<project><name>x</name></project>"
UTF16 = '<?xml version="1.0" encoding="UTF-16"?><r><a>x</a></r>'.encode("utf-16")
HTML_DOC = b"<!DOCTYPE html><html><body><p>hi</p></body></html>"
HTML_ROOT = b"<html><head><title>t</title></head><body>hello</body></html>"
DOCTYPE_XML = b'<!DOCTYPE note SYSTEM "note.dtd"><note><to>A</to></note>'
PLAIN = b"juste du texte sans structure, avec un < isole et 3 > 2"
MD_WITH_XML = b"# Exemple\n\nVoici un extrait :\n\n```xml\n<a><b>x</b></a>\n```\n"


def _ctx(name, ct=""):
    return ExtractionContext(filename=name, declared_content_type=ct)


class TestSignature:
    def test_xml_mime_constant_and_detector_registered(self):
        assert getattr(M, "XML", None) == XML_MIME
        names = [n for _, n in M.structured_detectors()]
        assert "xml" in names

    @pytest.mark.parametrize("data", [BASIC, NO_DECL, BOM_NO_DECL, UTF16, DOCTYPE_XML],
                             ids=["decl", "no-decl", "bom-comment", "utf16", "doctype"])
    def test_dot_xml_with_structural_signature_is_xml(self, data):
        assert M.detect_mime(data, _ctx("f.xml")) == XML_MIME

    def test_declared_content_type_is_never_the_proof(self):
        # texte brut nommé .xml avec un Content-Type xml : pas de signature -> pas XML
        assert M.detect_mime(PLAIN, _ctx("f.xml", "application/xml")) == M.PLAIN
        # signature XML mais extension d'un autre format supporté -> jamais XML
        assert M.detect_mime(BASIC, _ctx("f.txt", "application/xml")) == M.PLAIN
        assert M.detect_mime(BASIC, _ctx("f.md")) == M.MARKDOWN

    @pytest.mark.parametrize("name,data,expected", [
        ("page.html", HTML_DOC, M.HTML),
        ("page.htm", HTML_ROOT, M.HTML),
        ("notes.md", MD_WITH_XML, M.MARKDOWN),
        ("notes.txt", NO_DECL, M.PLAIN),
        ("image.svg", b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>', M.PLAIN),
        ("data.xsd", b'<?xml version="1.0"?><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>', M.PLAIN),
        ("style.xsl", b'<?xml version="1.0"?><xsl:stylesheet/>', M.PLAIN),
    ], ids=["html", "htm", "md", "txt", "svg", "xsd", "xsl"])
    def test_other_extensions_are_never_hijacked(self, name, data, expected):
        assert M.detect_mime(data, _ctx(name)) == expected

    def test_html_content_named_xml_stays_html(self):
        assert M.detect_mime(HTML_DOC, _ctx("page.xml")) == M.HTML
        assert M.detect_mime(HTML_ROOT, _ctx("page.xml")) == M.HTML

    @pytest.mark.parametrize("build,expected", [
        (lambda: b"\x7fELF\x02\x01\x01" + b"\x00" * 50, M.EXECUTABLE),
        (lambda: b"MZ\x90\x00" + b"\x00" * 50, M.EXECUTABLE),
        (make_png, M.PNG),
        (make_pdf_with_text, M.PDF),
        (make_docx, M.DOCX),
    ], ids=["elf", "pe", "png", "pdf", "docx"])
    def test_strong_signatures_named_xml_keep_their_type(self, build, expected):
        assert M.detect_mime(build(), _ctx("renamed.xml")) == expected

    def test_plain_text_named_xml_is_plain(self):
        assert M.detect_mime(PLAIN, _ctx("f.xml")) == M.PLAIN

    def test_malformed_xml_still_signed_as_xml(self):
        """La signature suffit à classer ; la malformation est refusée à l'extraction."""
        assert M.detect_mime(b"<root><a>ouvert", _ctx("f.xml")) == XML_MIME

    def test_detection_is_deterministic(self):
        assert [M.detect_mime(BASIC, _ctx("f.xml")) for _ in range(3)] == [XML_MIME] * 3


class TestGate:
    def test_disabled_by_default_is_a_controlled_rejection(self, settings):
        assert getattr(settings, "DOCUMENT_XML_ENABLED", False) is False
        with pytest.raises(RejectedFile) as ei:
            V.validate_and_classify(BASIC, _ctx("f.xml"))
        assert ei.value.code == "xml_ingestion_disabled"
        assert "XML" in ei.value.detail

    @XML_ON
    def test_enabled_classifies_as_xml_format(self):
        detected, fmt = V.validate_and_classify(BASIC, _ctx("f.xml"))
        assert detected == XML_MIME
        assert fmt == getattr(V, "FMT_XML", None) == "xml"

    @XML_ON
    def test_dependency_missing_is_controlled(self, monkeypatch):
        from apps.rag_core.services.ingest.extraction import xml_extractor as X

        monkeypatch.setattr(V, "xml_parser_available", lambda: False)
        with pytest.raises(RejectedFile) as ei:
            V.validate_and_classify(BASIC, _ctx("f.xml"))
        assert ei.value.code == "xml_dependency_unavailable"
        assert X.parser_available() is True  # l'environnement de test, lui, l'a

    @pytest.mark.parametrize("xml_on", [False, True])
    def test_other_formats_are_insensitive_to_the_flag(self, xml_on):
        with override_settings(DOCUMENT_XML_ENABLED=xml_on):
            assert V.validate_and_classify(b"hello plain", _ctx("a.txt"))[1] == V.FMT_TXT
            assert V.validate_and_classify(HTML_DOC, _ctx("a.html"))[1] == V.FMT_HTML
            assert V.validate_and_classify(make_docx(), _ctx("a.docx"))[1] == V.FMT_DOCX
            assert V.validate_and_classify(b"# T\nx", _ctx("a.md"))[1] == V.FMT_MARKDOWN

    @XML_ON
    def test_supported_formats_lists_xml_only_when_enabled(self):
        from apps.rag_core.services.ingest.extraction import supported_formats

        assert "xml" in supported_formats()
        with override_settings(DOCUMENT_XML_ENABLED=False):
            assert "xml" not in supported_formats()


class TestRetryability:
    @pytest.mark.parametrize("code", ["unsafe_xml", "xml_malformed", "xml_complexity_limit"])
    def test_definitive_codes(self, code):
        from apps.rag_core.services.ingest import retry_strategy as rs

        assert code in rs.NON_RETRYABLE_ERROR_CODES

    @pytest.mark.parametrize("code", ["xml_ingestion_disabled", "xml_dependency_unavailable"])
    def test_environmental_codes(self, code):
        from apps.rag_core.services.ingest import retry_strategy as rs

        assert code in rs.ENVIRONMENTAL_ERROR_CODES
