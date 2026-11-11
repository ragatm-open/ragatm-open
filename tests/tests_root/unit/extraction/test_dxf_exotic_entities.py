"""un DXF valide contenant une entité exotique non sémantique
(ezdxf la charge en `DXFTagStorage`, sans attribut `layer` supporté) ne doit pas être
rejeté : l'entité est comptée, observée (warning borné) et ignorée ; les entités
sémantiques du même fichier sont extraites normalement.

Fixture auto-suffisante : dessin ezdxf R2018 (LF) dans lequel une entité `SECTIONOBJECT`
(ou tout type inconnu) est injectée en tags bruts en tête de la section ENTITIES —
sous-classes `AcDbEntity` (avec groupe 8) + `AcDbSection`, comme un fichier AutoCAD réel.
ezdxf 1.4.4 n'a pas de classe pour ce type et le stocke tel quel ; la lecture stricte
l'accepte (prouvé par `TestFixture`), le handle injecté (≥ FFF1) ne collisionne avec aucun
handle généré.
"""
from __future__ import annotations

import io

import pytest
from django.test import override_settings

from apps.rag_core.services.ingest.extraction import ExtractionError, extract_document
from apps.rag_core.services.ingest.extraction import dxf_extractor as X

pytestmark = pytest.mark.usefixtures("_dxf_on")

UNKNOWN_TEXT = "Section Plane R2"  # texte porté par l'entité exotique : ne doit JAMAIS être indexé


@pytest.fixture
def _dxf_on():
    with override_settings(DOCUMENT_DXF_ENABLED=True):
        yield


def build_semantic_lf() -> bytes:
    """Dessin sémantique de référence : calque, TEXT, MTEXT, bloc à ATTRIB, cote, géométrie."""
    import ezdxf

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.layers.add("CVC")
    msp = doc.modelspace()
    msp.add_text("CTA-R2-01", dxfattribs={"layer": "CVC"})
    msp.add_mtext("{\\fArial|b1;Débit} 15000 m3/h\\Ptempérature 21 %%d", dxfattribs={"layer": "CVC"})
    blk = doc.blocks.new("EQUIP")
    blk.add_attdef("REPERE", (0, 0))
    blk.add_attdef("DEBIT", (0, 1))
    ins = msp.add_blockref("EQUIP", (10, 10), dxfattribs={"layer": "CVC"})
    ins.add_auto_attribs({"REPERE": "CTA-R2-01", "DEBIT": "15000 m3/h"})
    msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(2500, 0), dxfattribs={"layer": "CVC"}).render()
    for i in range(10):
        msp.add_line((i, 0), (i, 1), dxfattribs={"layer": "CVC"})
    s = io.StringIO()
    doc.write(s)
    data = s.getvalue().encode("utf-8")
    assert b"\r" not in data
    return data


def inject_unknown(
    data: bytes, dxftype: str = "SECTIONOBJECT", *, count: int = 1, first_handle: int = 0xFFF1,
    types: list[str] | None = None,
) -> bytes:
    """Insère en tête de ENTITIES `count` entités de type `dxftype` (ou une par type de `types`),
    en tags bruts : handle unique, propriétaire = block record *Model_Space, sous-classes
    AcDbEntity (groupe 8) + AcDbSection, un tag texte (groupe 1) qui ne doit jamais être indexé."""
    text = data.decode("utf-8")
    marker = "  0\nSECTION\n  2\nENTITIES\n"
    i = text.index(marker) + len(marker)
    owner = _model_space_handle(text)
    kinds = list(types) if types else [dxftype] * count
    ents = ""
    for k, kind in enumerate(kinds):
        ents += (
            f"  0\n{kind}\n  5\n{first_handle + k:X}\n330\n{owner}\n100\nAcDbEntity\n  8\n0\n"
            f"100\nAcDbSection\n 90\n1\n  1\n{UNKNOWN_TEXT}\n"
        )
    return (text[:i] + ents + text[i:]).encode("utf-8")


def _model_space_handle(text: str) -> str:
    import ezdxf

    return ezdxf.read(io.StringIO(text)).modelspace().block_record.dxf.handle


def to_crlf(lf: bytes) -> bytes:
    return lf.replace(b"\n", b"\r\n")


def _extract(data: bytes):
    return extract_document(data, filename="plan.dxf", content_type="application/dxf")


def _semantic(res) -> list:
    return [(u.text, u.meta) for u in res.units]


def _strict(data: bytes):
    import ezdxf

    return ezdxf.read(io.StringIO(data.decode("utf-8"), newline=None))


class TestFixture:
    """Garde §48/§49 : la fixture est un DXF strictement valide et l'entité est bien inconnue d'ezdxf."""

    def test_fixture_is_strict_valid_and_entity_is_tag_storage(self):
        from ezdxf.entities import DXFGraphic, DXFTagStorage

        d = _strict(inject_unknown(build_semantic_lf()))
        ex = [e for e in d.modelspace() if e.dxftype() == "SECTIONOBJECT"]
        assert len(ex) == 1
        e = ex[0]
        assert isinstance(e, DXFTagStorage) and not isinstance(e, DXFGraphic)
        assert e.dxf.is_supported("layer") is False and e.dxf.is_supported("handle") is True
        assert e.dxf.handle == "FFF1"

    def test_semantic_fixture_alone_is_extracted(self):
        res = _extract(build_semantic_lf())
        assert res.metadata["dxf_version"] == "AC1032"
        assert [u.meta["locator"]["path"] for u in res.units] == [
            "Drawing", "Layout:Model/Layer:CVC", "Layout:Model/Layer:CVC/Block:EQUIP",
        ]
        assert res.warnings == []
        assert res.metadata.get("unknown_entity_count", 0) == 0


class TestExoticEntityRobustness:
    def test_r2_1_valid_dxf_with_unknown_entity_is_not_rejected(self):
        res = _extract(inject_unknown(build_semantic_lf()))
        assert res.metadata["dxf_version"] == "AC1032"
        assert len(res.units) == 3

    def test_r2_2_text_mtext_attrib_dimension_survive(self):
        res = _extract(inject_unknown(build_semantic_lf()))
        joined = "\n".join(u.text for u in res.units)
        assert "CTA-R2-01" in joined and "Débit 15000 m3/h température 21 °" in joined
        assert "REPERE: CTA-R2-01" in joined and "DEBIT: 15000 m3/h" in joined
        assert "Dimension: 2500 mm" in joined

    def test_r2_3_unknown_entity_is_counted(self):
        res = _extract(inject_unknown(build_semantic_lf()))
        assert res.metadata["entity_counts"]["SECTIONOBJECT"] == 1
        assert res.metadata["unknown_entity_count"] == 1
        assert res.metadata["unknown_entity_counts"] == {"SECTIONOBJECT": 1}
        assert "SECTIONOBJECT: 1" in res.units[0].text  # résumé du dessin

    def test_r2_4_warning_is_emitted_once_and_bounded(self):
        res = _extract(inject_unknown(build_semantic_lf(), count=50))
        assert res.warnings.count("dxf_unknown_entities") == 1
        assert res.metadata["unknown_entity_count"] == 50
        assert res.metadata["unknown_entity_counts"] == {"SECTIONOBJECT": 50}
        assert len(res.units) == 3  # aucune unité par entité inconnue

    def test_r2_4b_many_unknown_types_keep_metadata_bounded(self):
        n_types = X.MAX_ENTITY_TYPES_RECORDED + 5
        res = _extract(inject_unknown(build_semantic_lf(), types=[f"ACME_UNKNOWN_{k:02d}" for k in range(n_types)]))
        assert res.warnings.count("dxf_unknown_entities") == 1
        assert res.metadata["unknown_entity_count"] == n_types
        assert len(res.metadata["unknown_entity_counts"]) == X.MAX_ENTITY_TYPES_RECORDED
        assert len(res.units) == 3

    def test_r2_5_no_unit_and_no_raw_tag_text_from_unknown_entity(self):
        res = _extract(inject_unknown(build_semantic_lf()))
        assert all("SECTIONOBJECT" not in u.meta["locator"]["path"] for u in res.units)
        assert all(UNKNOWN_TEXT not in u.text for u in res.units)

    def test_r2_6_semantic_output_unchanged_by_exotic_entity(self):
        lf = build_semantic_lf()
        a, b = _extract(lf), _extract(inject_unknown(lf))
        assert _semantic(a)[1:] == _semantic(b)[1:]  # annotations et bloc identiques
        assert b.units[0].text.replace("SECTIONOBJECT: 1, ", "") == a.units[0].text  # résumé : seul le compte diffère
        keys = {"entity_count", "entity_counts", "unknown_entity_count", "unknown_entity_counts", "warnings"}
        assert {k: v for k, v in a.metadata.items() if k not in keys} == {k: v for k, v in b.metadata.items() if k not in keys}
        assert b.metadata["entity_count"] == a.metadata["entity_count"] + 1
        assert set(b.warnings) - set(a.warnings) == {"dxf_unknown_entities"}

    def test_r2_6b_multiple_unknown_types_in_one_file(self):
        data = inject_unknown(inject_unknown(build_semantic_lf(), "SECTIONOBJECT"), "ACAD_PROXY_LIKE_XYZ", first_handle=0xFFE1)
        res = _extract(data)
        assert res.metadata["unknown_entity_counts"] == {"ACAD_PROXY_LIKE_XYZ": 1, "SECTIONOBJECT": 1}
        assert res.warnings.count("dxf_unknown_entities") == 1 and len(res.units) == 3

    @override_settings(DOCUMENT_DXF_MAX_ENTITIES=10_000)
    def test_r2_9_unknown_entities_count_toward_complexity_cap(self):
        lf = build_semantic_lf()
        known = _extract(lf).metadata["entity_count"]
        with override_settings(DOCUMENT_DXF_MAX_ENTITIES=known + 3):
            assert _extract(inject_unknown(lf, count=3)).metadata["entity_count"] == known + 3  # = cap : accepté
            with pytest.raises(ExtractionError) as ei:
                _extract(inject_unknown(lf, count=4))  # cap + 1
            assert ei.value.code == "dxf_complexity_limit"

    def test_r2_10_crlf_variant_with_exotic_entity(self):
        lf = inject_unknown(build_semantic_lf())
        a, b = _extract(lf), _extract(to_crlf(lf))
        assert _semantic(a) == _semantic(b) and a.metadata == b.metadata and a.warnings == b.warnings
        assert b.metadata["unknown_entity_counts"] == {"SECTIONOBJECT": 1}

    def test_r2_15_deterministic_counts_and_warnings(self):
        data = inject_unknown(inject_unknown(build_semantic_lf(), count=3), "ACAD_PROXY_LIKE_XYZ", count=2, first_handle=0xFFE1)
        a, b = _extract(data), _extract(data)
        assert a.metadata == b.metadata and a.warnings == b.warnings and _semantic(a) == _semantic(b)
        assert list(a.metadata["unknown_entity_counts"]) == sorted(a.metadata["unknown_entity_counts"])

    def test_r2_only_exotic_entities_keeps_informative_summary(self):
        """Politique existante (dessin sans annotation) : le résumé reste indexable, pas de document vide."""
        import ezdxf

        doc = ezdxf.new("R2018")
        s = io.StringIO()
        doc.write(s)
        res = _extract(inject_unknown(s.getvalue().encode("utf-8"), count=2))
        assert [u.meta["locator"]["path"] for u in res.units] == ["Drawing"]
        assert "SECTIONOBJECT: 2" in res.units[0].text and res.warnings == ["dxf_unknown_entities"]


class TestFailClosedUnchanged:
    def test_r2_7_malformed_dxf_is_still_rejected(self):
        data = inject_unknown(build_semantic_lf())
        with pytest.raises(ExtractionError) as ei:
            _extract(data[: len(data) // 2])
        assert ei.value.code == "dxf_malformed"
        with pytest.raises(ExtractionError) as ei:
            _extract(data.replace(b"\nAcDbSection\n", b"\nAcDbSection\ngarbage\n"))  # code de groupe invalide
        assert ei.value.code == "dxf_malformed"

    def test_r2_8_known_semantic_entity_failure_stays_fail_closed(self, monkeypatch):
        """Une erreur d'attribut sur une entité sémantique connue n'est pas « avalée » comme inconnue."""
        from ezdxf.lldxf.const import DXFAttributeError

        def boom(e, warnings):
            raise DXFAttributeError("Invalid DXF attribute \"text\" for entity TEXT")

        monkeypatch.setattr(X, "_entity_text", boom)
        with pytest.raises(ExtractionError) as ei:
            _extract(inject_unknown(build_semantic_lf()))
        assert ei.value.code == "dxf_malformed"

    def test_r2_9b_cap_exceeded_with_exotic_entities_is_complexity_not_malformed(self):
        with override_settings(DOCUMENT_DXF_MAX_ENTITIES=5):
            with pytest.raises(ExtractionError) as ei:
                _extract(inject_unknown(build_semantic_lf(), count=20))
            assert ei.value.code == "dxf_complexity_limit"

    def test_r2_11_binary_dxf_unchanged(self):
        import ezdxf

        doc = ezdxf.new("R2018")
        doc.modelspace().add_text("BIN-R2", dxfattribs={"layer": "0"})
        b = io.BytesIO()
        doc.write(b, fmt="bin")
        res = _extract(b.getvalue())
        assert res.metadata["dxf_binary"] is True and "BIN-R2" in "\n".join(u.text for u in res.units)
        assert res.warnings == []

    def test_pre_r2007_codepage_unchanged(self):
        import ezdxf

        doc = ezdxf.new("R2000")
        doc.modelspace().add_text("Débit réglé à 12000 m³", dxfattribs={"layer": "0"})
        s = io.StringIO()
        doc.write(s)
        res = _extract(s.getvalue().encode(doc.encoding))
        assert res.metadata["dxf_version"] == "AC1015"
        assert "Débit réglé à 12000 m³" in "\n".join(u.text for u in res.units)
