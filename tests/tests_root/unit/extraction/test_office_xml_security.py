"""Lot E0 — durcissement XML partagé (DOCTYPE / ENTITY) sur les documents Office.

`xml.etree.ElementTree` (expat) N'EST PAS immunisé contre l'expansion d'entités
INTERNES (« billion laughs »). Et la garde zip-bomb ne voit rien passer : une bombe
à entités pèse quelques kilo-octets une fois décompressée — elle n'explose qu'À
L'INTÉRIEUR du parseur.

Donc : on refuse la DÉCLARATION, avant tout parsing.
"""
import pytest

from apps.rag_core.services.ingest.extraction import RejectedFile, extract_document
from apps.rag_core.services.ingest.extraction.xml_security import (
    is_unsafe_xml,
    reject_unsafe_xml,
)

from ._builders import (
    make_docx,
    make_docx_mentioning_doctype_in_text,
    make_docx_with_unsafe_xml,
)


class TestUnsafeXmlDetector:
    @pytest.mark.parametrize(
        "payload",
        [
            b'<!DOCTYPE foo>',
            b'<!doctype foo>',
            b'<!DoCtYpE foo>',
            b'<!ENTITY x "y">',
            b'<!entity x "y">',
            b'<!  DOCTYPE foo>',          # espaces après "<!"
            b'<!\n\tENTITY x "y">',       # retours à la ligne après "<!"
            b'<?xml version="1.0"?>\n<!DOCTYPE r [ <!ENTITY a "b"> ]>\n<r/>',
        ],
    )
    def test_detects_declarations(self, payload):
        assert is_unsafe_xml(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            '<?xml version="1.0"?><r><t>Un DOCTYPE se déclare en XML.</t></r>'.encode(),
            b"<r><t>ENTITY est un mot anglais</t></r>",
            b"<!-- commentaire mentionnant DOCTYPE et ENTITY -->",
            b'<r attr="DOCTYPE"/>',
        ],
    )
    def test_no_false_positive_on_plain_words(self, payload):
        """La garde matche la SYNTAXE de déclaration, pas le vocabulaire."""
        assert is_unsafe_xml(payload) is False

    def test_reject_raises_typed_error_without_leaking_content(self):
        secret = b'<?xml version="1.0"?><!DOCTYPE r><r>SECRET_INTERNE_42</r>'
        with pytest.raises(RejectedFile) as ei:
            reject_unsafe_xml(secret, part_name="word/document.xml")
        assert ei.value.code == "unsafe_xml"
        # Aucun contenu XML, aucun chemin système, aucune trace dans le message.
        assert "SECRET_INTERNE_42" not in ei.value.detail
        assert "<" not in ei.value.detail
        assert "/home" not in ei.value.detail
        # La partie concernée reste disponible pour le diagnostic (constante du format).
        assert ei.value.meta == {"unsafe_xml_part": "word/document.xml"}

    def test_reject_is_noop_on_safe_xml(self):
        reject_unsafe_xml(b"<?xml version='1.0'?><r/>", part_name="word/document.xml")


class TestDocxUnsafeXml:
    @pytest.mark.parametrize("kind", ["doctype", "entity", "billion_laughs", "spaced"])
    def test_docx_with_unsafe_xml_is_refused(self, kind):
        with pytest.raises(RejectedFile) as ei:
            extract_document(make_docx_with_unsafe_xml(kind), filename="piege.docx")
        assert ei.value.code == "unsafe_xml"

    def test_refusal_happens_before_parsing(self, monkeypatch):
        """Preuve que la garde est une PRÉ-condition : le parseur n'est jamais atteint."""
        from xml.etree import ElementTree as ET

        from apps.rag_core.services.ingest.extraction import docx_extractor

        # Assertion d'ABSENCE : elle doit rester capable d'échouer malgré la frontière
        # de processus (HARDENING-03), sans quoi une garde de sécurité deviendrait
        # décorative. Le témoin franchit la frontière.
        from tests_root.process_witness import TemoinPartage

        called = TemoinPartage(prefixe="docx-xml-parse-")

        def _boom(*a, **kw):
            called.note()
            raise AssertionError("ET.fromstring ne doit PAS être appelé sur un XML piégé")

        monkeypatch.setattr(docx_extractor.ET, "fromstring", _boom)

        with pytest.raises(RejectedFile) as ei:
            extract_document(
                make_docx_with_unsafe_xml("billion_laughs"), filename="piege.docx"
            )
        assert ei.value.code == "unsafe_xml"
        assert called.valeurs == []
        assert ET is not None  # (import utilisé pour la lisibilité du test)

    def test_normal_docx_still_extracted(self):
        r = extract_document(make_docx(title="Titre", paragraphs=("Para 1",)),
                             filename="ok.docx")
        assert r.status.value == "success"
        assert "Para 1" in r.text

    def test_docx_mentioning_doctype_in_text_is_accepted(self):
        """Faux positif interdit : un document QUI PARLE de DOCTYPE reste indexable."""
        r = extract_document(make_docx_mentioning_doctype_in_text(), filename="cours.docx")
        assert r.status.value == "success"
        assert "DOCTYPE" in r.text
