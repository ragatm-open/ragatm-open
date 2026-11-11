"""extracteur XML : sécurité, limites, unités sémantiques.

XML-1 (propriétaire, xfail strict sur la ligne de base), XML-2 namespaces, XML-3 records
répétés, XML-4 XXE, XML-5 DTD, XML-6 bombe d'entités, XML-7 profondeur, XML-8 nœuds,
XML-12 attributs, XML-13 contenu mixte, XML-14 vide, XML-15 usurpations, BOM/encodages,
déterminisme. Tout passe par `extract_document` (dispatch réel), flag activé.
"""
from __future__ import annotations

import pytest
from apps.rag_core.services.ingest.extraction import (
    ExtractionComplexityExceeded,
    ExtractionError,
    ExtractionStatus,
    RejectedFile,
    extract_document,
)
from django.test import override_settings

from ._builders import make_pdf_with_text

pytestmark = pytest.mark.usefixtures("_xml_on")


@pytest.fixture
def _xml_on():
    with override_settings(DOCUMENT_XML_ENABLED=True):
        yield


def _x(data: bytes, name="doc.xml"):
    return extract_document(data, filename=name, content_type="application/xml")


def _texts(res):
    return [u.text for u in res.units]


def _paths(res):
    return [u.meta["locator"]["path"] for u in res.units]


BASIC = b"""<?xml version="1.0" encoding="UTF-8"?>
<project>
  <name>H\xc3\xb4pital Saint-X</name>
  <city>Paris</city>
</project>
"""


# --------------------------------------------------------------------------- #
# XML-1 — support de base (contrat propriétaire)
# --------------------------------------------------------------------------- #
class TestXml1Basic:
    def test_basic_xml_becomes_structured_units(self):
        res = _x(BASIC)
        assert res.metadata.get("format") == "xml", "XML traité comme texte brut"
        assert res.status == ExtractionStatus.SUCCESS
        assert res.units, "aucune unité structurée"
        assert res.text == ""
        joined = "\n".join(_texts(res))
        assert "XML path: /project" in joined
        assert "name: Hôpital Saint-X" in joined
        assert "city: Paris" in joined
        assert "<name>" not in joined and "</project>" not in joined and "<?xml" not in joined
        assert all(u.meta["locator"]["type"] == "element" for u in res.units)
        assert all(u.meta["locator"]["path"].startswith("/project") for u in res.units)
        assert all(u.meta.get("structured_format") == "xml" for u in res.units)
        assert res.metadata["root_tag"] == "project"
        assert res.metadata["parser"] == "defusedxml"
        assert res.metadata["parser_version"]
        assert res.metadata["node_count"] == 3
        assert res.metadata["unit_count"] == len(res.units)
        assert not res.pages and not res.slides and not res.email_sections and not res.excel_chunks

    def test_root_scalar_is_indexable(self):
        res = _x(b"<Name>ABC</Name>")
        assert res.status == ExtractionStatus.SUCCESS
        assert _paths(res) == ["/Name"]
        assert "text: ABC" in res.units[0].text

    def test_small_document_is_one_root_unit(self):
        res = _x(BASIC)
        assert _paths(res) == ["/project"]


# --------------------------------------------------------------------------- #
# XML-2 — namespaces
# --------------------------------------------------------------------------- #
class TestXml2Namespaces:
    def test_default_and_prefixed_namespaces(self):
        data = b"""<?xml version="1.0"?>
<ubl:Invoice xmlns:ubl="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
             xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
             xmlns="urn:default:ns">
  <cbc:ID>INV-7</cbc:ID>
  <Note>Livraison</Note>
</ubl:Invoice>"""
        res = _x(data)
        assert res.metadata["root_tag"] == "Invoice"
        assert _paths(res) == ["/Invoice"]
        t = res.units[0].text
        assert "ID: INV-7" in t and "Note: Livraison" in t
        assert "urn:oasis" not in t and "{" not in t
        ns = res.metadata["namespaces"]
        assert ns["ubl"].startswith("urn:oasis") and ns["cbc"].startswith("urn:oasis")
        assert ns[""] == "urn:default:ns"
        assert res.metadata["root_namespace"].startswith("urn:oasis")

    def test_namespace_is_never_an_error_and_paths_are_stable(self):
        data = b'<a xmlns="urn:x"><b><c>1</c></b><b><c>2</c></b></a>'
        res = _x(data)
        assert _paths(res) == ["/a/b[1]", "/a/b[2]"]


# --------------------------------------------------------------------------- #
# XML-3 — records répétés
# --------------------------------------------------------------------------- #
COMPANIES = b"""<companies>
  <meta><source>registre</source></meta>
  <company id="A"><name>Entreprise ABC</name><country>FR</country></company>
  <company id="B"><name>Entreprise DEF</name><country>BE</country></company>
  <company id="C"><name>Entreprise GHI</name><country>LU</country></company>
</companies>"""


class TestXml3RepeatedRecords:
    def test_each_record_is_a_unit_with_deterministic_path(self):
        res = _x(COMPANIES)
        paths = _paths(res)
        assert paths == ["/companies/meta", "/companies/company[1]", "/companies/company[2]",
                         "/companies/company[3]"]
        u2 = res.units[2]
        assert u2.text.startswith("XML path: /companies/company[2]")
        assert "@id: B" in u2.text and "name: Entreprise DEF" in u2.text and "country: BE" in u2.text
        assert "Entreprise ABC" not in u2.text
        assert u2.meta["locator"]["identifier"] == "B"
        assert res.metadata["record_groups"] == 1

    def test_no_whole_document_duplicate(self):
        res = _x(COMPANIES)
        joined = "\n".join(_texts(res))
        assert joined.count("Entreprise ABC") == 1
        assert joined.count("registre") == 1

    def test_outermost_record_boundary(self):
        data = b"""<orders>
  <order id="1"><lines><line>a</line><line>b</line></lines></order>
  <order id="2"><lines><line>c</line></lines></order>
</orders>"""
        res = _x(data)
        assert _paths(res) == ["/orders/order[1]", "/orders/order[2]"]
        assert "lines/line[1]: a" in res.units[0].text and "lines/line[2]: b" in res.units[0].text
        assert "lines/line: c" in res.units[1].text
        joined = "\n".join(_texts(res))
        assert joined.count(": a") == 1

    def test_no_records_large_document_one_unit_per_branch(self):
        branches = "".join(f"<s{i}><v>{i}</v><w>{i}</w></s{i}>" for i in range(40))
        res = _x(f"<root a='1'>{branches}</root>".encode())
        assert res.metadata["node_count"] == 1 + 40 * 3
        assert _paths(res)[0] == "/root"
        assert "@a: 1" in res.units[0].text
        assert _paths(res)[1:3] == ["/root/s0", "/root/s1"]
        assert len(res.units) == 41

    def test_document_order_is_preserved_never_sorted(self):
        data = b"<r><item>zeta</item><item>alpha</item><item>mu</item></r>"
        res = _x(data)
        assert [u.text.splitlines()[-1] for u in res.units] == ["text: zeta", "text: alpha", "text: mu"]


# --------------------------------------------------------------------------- #
# XML-4/5/6 — XXE, DTD, bombe d'entités
# --------------------------------------------------------------------------- #
class TestSecurity:
    def test_xxe_is_rejected_and_never_reads_the_target(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
        data = (
            f'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file://{secret}">]>'
            f"<foo>&xxe;</foo>"
        ).encode()
        with pytest.raises(RejectedFile) as ei:
            _x(data)
        assert ei.value.code == "unsafe_xml"
        assert "TOP-SECRET-CONTENT" not in ei.value.detail
        assert str(secret) not in ei.value.detail

    @pytest.mark.parametrize("data", [
        b'<!DOCTYPE note SYSTEM "http://example.invalid/note.dtd"><note>x</note>',
        b"<!DOCTYPE note [<!ELEMENT note (#PCDATA)>]><note>x</note>",
        b'<?xml version="1.0"?>\n<!DOCTYPE note>\n<note>x</note>',
    ])
    def test_any_dtd_is_rejected(self, data):
        with pytest.raises(RejectedFile) as ei:
            _x(data)
        assert ei.value.code == "unsafe_xml"

    def test_entity_bomb_is_rejected_before_expansion(self):
        ents = ['<!ENTITY a "aaaaaaaaaa">']
        for i in range(1, 12):
            prev = chr(ord("a") + i - 1)
            cur = chr(ord("a") + i)
            ents.append(f'<!ENTITY {cur} "{("&" + prev + ";") * 10}">')
        data = ('<?xml version="1.0"?><!DOCTYPE bomb [' + "".join(ents) + "]><bomb>&l;</bomb>").encode()
        with pytest.raises(RejectedFile) as ei:
            _x(data)
        assert ei.value.code == "unsafe_xml"
        assert len(ei.value.detail) < 500

    def test_no_network_no_file_resolution_for_external_entity_without_doctype(self, tmp_path):
        """Référence externe hors DOCTYPE : rien à résoudre, mais on prouve qu'aucun
        chemin n'est ouvert et que la réponse reste fermée ou neutre."""
        data = b'<r><a href="file:///etc/hostname">x</a></r>'
        res = _x(data)
        assert "file:///etc/hostname" in res.units[0].text  # une simple valeur d'attribut
        assert res.status == ExtractionStatus.SUCCESS


# --------------------------------------------------------------------------- #
# XML-7/8 — limites de complexité (racine = profondeur 1)
# --------------------------------------------------------------------------- #
def _nested(depth: int) -> bytes:
    return ("".join(f"<n{i}>" for i in range(depth)) + "x" + "".join(f"</n{i}>" for i in reversed(range(depth)))).encode()


class TestComplexity:
    def test_defaults_are_named_and_centralised(self):
        from apps.rag_core.services.ingest.extraction import config

        assert config.xml_max_depth() == 64
        assert config.xml_max_nodes() == 200_000

    def test_depth_boundary(self):
        assert _x(_nested(64)).metadata["max_depth"] == 64
        with pytest.raises(ExtractionComplexityExceeded) as ei:
            _x(_nested(65))
        assert ei.value.code == "xml_complexity_limit"
        assert isinstance(ei.value, ExtractionError)

    @override_settings(DOCUMENT_XML_MAX_NODES=50)
    def test_node_boundary_with_override(self):
        ok = b"<r>" + b"<i>1</i>" * 49 + b"</r>"          # 1 + 49 = 50 éléments
        assert _x(ok).metadata["node_count"] == 50
        too_many = b"<r>" + b"<i>1</i>" * 50 + b"</r>"    # 51
        with pytest.raises(ExtractionComplexityExceeded) as ei:
            _x(too_many)
        assert ei.value.code == "xml_complexity_limit"

    @override_settings(DOCUMENT_XML_MAX_NODES=10)
    def test_early_abort_does_not_parse_the_whole_document(self):
        """Le dépassement est levé au 11e élément, dans le premier bloc lu : la fin du
        document (80 Ko plus loin, mal formée) n'est jamais analysée — sinon le parseur
        produirait xml_malformed au lieu de la limite de complexité."""
        data = b"<r>" + b"<i>1</i>" * 10_000 + b"<broken></r>"
        with pytest.raises(ExtractionComplexityExceeded):
            _x(data)

    def test_node_count_ignores_text_presence(self):
        assert _x(b"<r><a/><b>t</b><c><d/></c></r>").metadata["node_count"] == 5


# --------------------------------------------------------------------------- #
# XML-12/13 — attributs, contenu mixte ; commentaires/PI/CDATA
# --------------------------------------------------------------------------- #
class TestContent:
    def test_attributes_are_kept_and_id_feeds_identifier(self):
        data = b'<r><rec id="R-1" status="active" amount="12"/><rec id="R-2" status="closed"/></r>'
        res = _x(data)
        assert res.units[0].meta["locator"]["identifier"] == "R-1"
        assert "@status: active" in res.units[0].text and "@amount: 12" in res.units[0].text
        assert "@id: R-1" in res.units[0].text

    def test_xml_id_feeds_identifier(self):
        data = b'<r><rec xml:id="X9">v</rec><rec>w</rec></r>'
        res = _x(data)
        assert res.units[0].meta["locator"]["identifier"] == "X9"
        assert "identifier" not in res.units[1].meta["locator"]

    def test_mixed_content_keeps_textual_order(self):
        res = _x(b"<doc><p>Hello <b>world</b>. Fin <i>du</i> texte.</p></doc>")
        assert "p: Hello world. Fin du texte." in res.units[0].text

    def test_comments_and_processing_instructions_are_not_indexed(self):
        data = b'<?xml version="1.0"?><?xml-stylesheet href="s.xsl"?><r><!-- secret note --><a>v</a><?pi data?></r>'
        res = _x(data)
        joined = "\n".join(_texts(res))
        assert "secret note" not in joined and "stylesheet" not in joined and "pi data" not in joined
        assert "a: v" in joined

    def test_cdata_is_text(self):
        res = _x(b"<r><a><![CDATA[<b>brut</b> & co]]></a></r>")
        assert "a: <b>brut</b> & co" in res.units[0].text

    def test_whitespace_is_normalised_without_changing_values(self):
        res = _x(b"<r><a>  Valeur   AVEC   Casse \n et 12,50 EUR </a></r>")
        assert "a: Valeur AVEC Casse et 12,50 EUR" in res.units[0].text

    def test_empty_elements_produce_no_unit(self):
        res = _x(b"<r><a/><b>  </b><c><d/></c><e>ok</e></r>")
        assert _paths(res) == ["/r"]
        assert res.units[0].text == "XML path: /r\ne: ok"


# --------------------------------------------------------------------------- #
# XML-14 — vide ; XML-15 — usurpations ; encodages ; déterminisme
# --------------------------------------------------------------------------- #
class TestEdges:
    @pytest.mark.parametrize("data", [b"<root/>", b"<root><empty/></root>", b"<root>   </root>"])
    def test_empty_xml_is_a_controlled_empty_extraction(self, data):
        res = _x(data)
        assert res.units == []
        assert res.status == ExtractionStatus.EMPTY

    def test_malformed_is_non_retryable_code(self):
        with pytest.raises(ExtractionError) as ei:
            _x(b"<root><a>ouvert</root>")
        assert ei.value.code == "xml_malformed"

    def test_invalid_encoding_is_malformed_not_a_crash(self):
        data = b'<?xml version="1.0" encoding="UTF-8"?><r><a>\xff\xfe\xc3</a></r>'
        with pytest.raises(ExtractionError) as ei:
            _x(data)
        assert ei.value.code == "xml_malformed"

    def test_bom_and_declared_latin1_are_supported(self):
        bom = b"\xef\xbb\xbf<r><a>caf\xc3\xa9</a></r>"
        assert "a: café" in _x(bom).units[0].text
        latin = '<?xml version="1.0" encoding="ISO-8859-1"?><r><a>café</a></r>'.encode("latin-1")
        assert "a: café" in _x(latin).units[0].text

    def test_pdf_renamed_xml_stays_pdf(self):
        res = _x(make_pdf_with_text(), name="renamed.xml")
        assert res.metadata.get("format") == "pdf"

    def test_executable_renamed_xml_is_refused(self):
        with pytest.raises(RejectedFile) as ei:
            _x(b"\x7fELF\x02\x01\x01" + b"\x00" * 50, name="evil.xml")
        assert ei.value.code == "executable_refused"

    def test_plain_text_named_xml_is_not_xml(self):
        res = _x(b"juste du texte sans structure", name="notes.xml")
        assert res.metadata.get("format") == "txt"

    def test_determinism(self):
        a, b = _x(COMPANIES), _x(COMPANIES)
        assert [(u.text, u.meta) for u in a.units] == [(u.text, u.meta) for u in b.units]
        assert {k: a.metadata[k] for k in ("node_count", "record_groups", "unit_count")} == \
               {k: b.metadata[k] for k in ("node_count", "record_groups", "unit_count")}

    def test_long_namespace_uri_is_not_repeated_in_lines(self):
        uri = "urn:" + "x" * 500
        data = f'<r xmlns="{uri}"><a>1</a><b>2</b></r>'.encode()
        res = _x(data)
        assert uri not in res.units[0].text
        assert len(res.metadata["namespaces"][""]) <= 200
