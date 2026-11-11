"""un DXF ASCII est le même document en LF et en CRLF.

Défaut corrigé : le chemin en mémoire (`_decode_ascii` → `io.StringIO` → `ezdxf.read`)
conservait le `\\r` de fin de ligne ; `AC1032\\r` n'était pas reconnu, ezdxf retombait sur
un document R12 vide — 0 unité, aucune erreur. Les octets LF sont générés UNE fois par
ezdxf, les variantes CRLF / CR en sont dérivées déterministement (AutoCAD-like : mêmes
paires code/valeur, seules les fins de ligne changent).
"""
from __future__ import annotations

import io

import pytest
from django.test import override_settings

from apps.rag_core.services.ingest.extraction import ExtractionError, extract_document
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.validation import FMT_DXF, validate_and_classify

pytestmark = pytest.mark.usefixtures("_dxf_on")


@pytest.fixture
def _dxf_on():
    with override_settings(DOCUMENT_DXF_ENABLED=True):
        yield


def build_lf() -> bytes:
    """DXF R2018 (AC1032) : calque, TEXT, MTEXT formaté avec accent et °, bloc à ATTRIB,
    cote linéaire, géométrie. Écrit par ezdxf en LF pur."""
    import ezdxf

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.layers.add("CVC")
    msp = doc.modelspace()
    msp.add_text("CTA-CRLF-01", dxfattribs={"layer": "CVC"})
    msp.add_mtext("{\\fArial|b1;Débit} 15000 m3/h\\Ptempérature 21 %%d", dxfattribs={"layer": "CVC"})
    blk = doc.blocks.new("EQUIP")
    blk.add_attdef("REPERE", (0, 0))
    blk.add_attdef("DEBIT", (0, 1))
    ins = msp.add_blockref("EQUIP", (10, 10), dxfattribs={"layer": "CVC"})
    ins.add_auto_attribs({"REPERE": "CTA-CRLF-01", "DEBIT": "15000 m3/h"})
    msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(2500, 0), dxfattribs={"layer": "CVC"}).render()
    for i in range(20):
        msp.add_line((i, 0), (i, 1), dxfattribs={"layer": "CVC"})
    lay = doc.layouts.new("RDC")
    lay.add_text("Tableau TGBT", dxfattribs={"layer": "0"})
    s = io.StringIO()
    doc.write(s)
    data = s.getvalue().encode("utf-8")
    assert b"\r" not in data
    return data


def to_crlf(lf: bytes) -> bytes:
    return lf.replace(b"\n", b"\r\n")


def to_cr(lf: bytes) -> bytes:
    return lf.replace(b"\n", b"\r")


def _extract(data: bytes):
    return extract_document(data, filename="plan.dxf", content_type="application/dxf")


def _semantic(res) -> dict:
    """Vue sémantique complète : unités (texte + méta) et métadonnées d'extraction."""
    return {
        "units": [(u.text, u.meta) for u in res.units],
        "metadata": res.metadata,
        "warnings": list(res.warnings),
    }


class TestLineEndingParity:
    def test_crlf_yields_the_same_document_as_lf(self):
        lf = build_lf()
        a, b = _extract(lf), _extract(to_crlf(lf))
        assert a.metadata["dxf_version"] == "AC1032"
        assert b.metadata["dxf_version"] == "AC1032", "CRLF : version mal lue (repli R12)"
        assert len(b.units) == len(a.units) > 0
        assert _semantic(b) == _semantic(a)

    def test_crlf_is_not_a_silent_empty_extraction(self):
        lf = build_lf()
        res = _extract(to_crlf(lf))
        joined = "\n".join(u.text for u in res.units)
        assert res.units, "CRLF : 0 unité sans erreur (symptôme d'origine)"
        assert "CTA-CRLF-01" in joined and "15000 m3/h" in joined

    def test_text_mtext_block_attrib_dimension_layer_layout_locator_parity(self):
        lf = build_lf()
        a, b = _extract(lf), _extract(to_crlf(lf))
        paths_a = [u.meta["locator"]["path"] for u in a.units]
        paths_b = [u.meta["locator"]["path"] for u in b.units]
        assert paths_b == paths_a
        assert "Layout:Model/Layer:CVC/Block:EQUIP" in paths_b and "Layout:RDC/Layer:0" in paths_b
        grp = next(u for u in b.units if u.meta["locator"]["path"] == "Layout:Model/Layer:CVC")
        assert "CTA-CRLF-01" in grp.text and "Débit 15000 m3/h température 21 °" in grp.text
        assert "Dimension: 2500 mm" in grp.text
        blk = next(u for u in b.units if u.meta["locator"]["path"].endswith("Block:EQUIP"))
        assert "REPERE: CTA-CRLF-01" in blk.text and "DEBIT: 15000 m3/h" in blk.text
        assert b.metadata["layouts"] == a.metadata["layouts"] == ["Model", "Layout1", "RDC"]
        assert b.metadata["layers"] == a.metadata["layers"]
        assert b.metadata["entity_counts"] == a.metadata["entity_counts"]
        assert b.metadata["units_name"] == "Millimeters"

    def test_lone_cr_is_read_like_lf(self):
        """Fins de ligne CR seules (universal newlines) : même document, sans coût.
        Avant correctif : tout le fichier tient sur une ligne → `dxf_malformed`."""
        lf = build_lf()
        try:
            res = _extract(to_cr(lf))
        except ExtractionError as e:  # pragma: no cover - chemin d'avant correctif
            raise AssertionError(f"CR seul rejeté : {e.code}") from e
        assert _semantic(res) == _semantic(_extract(lf))

    def test_pre_r2007_codepage_crlf_keeps_accents(self):
        import ezdxf

        doc = ezdxf.new("R2000")
        doc.modelspace().add_text("Débit réglé à 12000 m³", dxfattribs={"layer": "0"})
        s = io.StringIO()
        doc.write(s)
        lf = s.getvalue().encode(doc.encoding)  # cp1252
        assert b"\r" not in lf
        a, b = _extract(lf), _extract(to_crlf(lf))
        assert a.metadata["dxf_version"] == b.metadata["dxf_version"] == "AC1015"
        assert "Débit réglé à 12000 m³" in "\n".join(u.text for u in b.units)
        assert _semantic(a) == _semantic(b)

    @override_settings(DOCUMENT_DXF_MAX_ENTITIES=10)
    def test_complexity_limit_applies_identically_under_crlf(self):
        def code(data):
            try:
                _extract(data)
            except ExtractionError as e:
                return e.code
            return None

        lf = build_lf()
        assert code(to_crlf(lf)) == code(lf) == "dxf_complexity_limit"


class TestUnchangedBehaviour:
    def test_lf_document_still_correct(self):
        res = _extract(build_lf())
        assert res.metadata["dxf_version"] == "AC1032" and len(res.units) == 4
        assert res.metadata["entity_counts"]["LINE"] >= 20  # + lignes rendues de la cote

    def test_crlf_detection_and_classification_unchanged(self):
        data = to_crlf(build_lf())
        ctx = ExtractionContext(filename="plan.dxf", declared_content_type="application/octet-stream")
        assert M.detect_mime(data, ctx) == M.DXF == "application/dxf"
        assert validate_and_classify(data, ctx)[1] == FMT_DXF

    def test_malformed_crlf_is_still_rejected(self):
        data = to_crlf(build_lf())
        with pytest.raises(ExtractionError) as ei:
            _extract(data[: len(data) // 2])
        assert ei.value.code == "dxf_malformed"
        with pytest.raises(ExtractionError) as ei:
            _extract(b"  0\r\nSECTION\r\n  2\r\nHEADER\r\nxx\r\n")
        assert ei.value.code == "dxf_malformed"

    def test_binary_dxf_unchanged(self):
        import ezdxf

        doc = ezdxf.new("R2018")
        doc.modelspace().add_text("BIN-CRLF", dxfattribs={"layer": "0"})
        b = io.BytesIO()
        doc.write(b, fmt="bin")
        res = _extract(b.getvalue())
        assert res.metadata["dxf_binary"] is True and "BIN-CRLF" in "\n".join(u.text for u in res.units)
