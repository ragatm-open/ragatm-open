"""extracteur DXF : sémantique CAO documentaire, sans géométrie.

DXF-1 (propriétaire, xfail strict sur la base), DXF-2 layouts/calques, DXF-3 bloc +
attributs, DXF-4 cotes, DXF-5/5B XREF, DXF-6 malformé, DXF-7 complexité, DXF-11 calque
long, DXF-12 bruit géométrique, DXF-13 binaire, DXF-14 usurpations, DXF-17 MTEXT,
DXF-18 vide, DXF-19 déterminisme. Tout passe par `extract_document`, flag activé.
"""
from __future__ import annotations

import re

import pytest
from apps.rag_core.services.ingest.extraction import (
    ExtractionComplexityExceeded,
    ExtractionError,
    ExtractionStatus,
    RejectedFile,
    extract_document,
)
from django.test import override_settings

from . import _dxf_builders as B
from ._builders import make_pdf_with_text

pytestmark = pytest.mark.usefixtures("_dxf_on")


@pytest.fixture
def _dxf_on():
    with override_settings(DOCUMENT_DXF_ENABLED=True):
        yield


def _x(data: bytes, name="plan.dxf"):
    return extract_document(data, filename=name, content_type="application/dxf")


def _texts(res):
    return [u.text for u in res.units]


def _paths(res):
    return [u.meta["locator"]["path"] for u in res.units]


def _unit(res, path_prefix):
    return next(u for u in res.units if u.meta["locator"]["path"].startswith(path_prefix))


GROUP_CODE_DUMP = re.compile(r"(?m)^\s*(0|8|10|20|30)\s*$")
COORDS = re.compile(r"\b[XYZ]\s*=\s*-?\d")


# --------------------------------------------------------------------------- #
# DXF-1 — support de base (contrat propriétaire)
# --------------------------------------------------------------------------- #
class TestDxf1Basic:
    def test_basic_dxf_becomes_structured_units(self):
        res = _x(B.build_basic())
        assert res.metadata.get("format") == "dxf", "DXF traité comme texte brut"
        assert res.status == ExtractionStatus.SUCCESS
        assert res.text == ""
        assert res.units, "aucune unité"
        joined = "\n".join(_texts(res))
        assert "CTA-03" in joined and "Débit 12000 m3/h" in joined
        assert not GROUP_CODE_DUMP.search(joined)
        assert all(u.meta["locator"]["type"] == "dxf" for u in res.units)
        assert all(u.meta.get("structured_format") == "dxf" for u in res.units)
        assert res.metadata["parser"] == "ezdxf" and res.metadata["parser_version"]
        assert res.metadata["dxf_version"] == "AC1032"
        assert res.metadata["entity_counts"] == {"LINE": 1, "MTEXT": 1, "TEXT": 1}
        assert res.metadata["semantic_unit_count"] == len(res.units)

    def test_summary_and_annotation_group(self):
        res = _x(B.build_basic())
        assert res.units and res.metadata.get("format") == "dxf", "DXF traité comme texte brut"
        assert _paths(res)[0] == "Drawing"
        summary = res.units[0].text
        assert summary.startswith("DXF drawing\n")
        assert "Version: AC1032 (R2018)" in summary and "Units: Millimeters" in summary
        assert "Layouts: Model" in summary and "CVC" in summary
        assert "LINE: 1" in summary and "TEXT: 1" in summary
        grp = _unit(res, "Layout:Model/Layer:CVC")
        assert grp.text == "DXF annotations\nLayout: Model\nLayer: CVC\n\nCTA-03\nDébit 12000 m3/h"
        assert grp.meta["locator"] == {"type": "dxf", "path": "Layout:Model/Layer:CVC"}


# --------------------------------------------------------------------------- #
# DXF-2 — layouts / calques
# --------------------------------------------------------------------------- #
class TestDxf2LayoutsLayers:
    def test_context_is_correct_and_deterministic(self):
        res = _x(B.build_layouts())
        paths = _paths(res)
        assert paths == [
            "Drawing",
            "Layout:Model/Layer:CVC", "Layout:Model/Layer:STRUCTURE",
            "Layout:RDC/Layer:ELEC_CFO",
            "Layout:R+1/Layer:CVC",
        ]
        cvc = _unit(res, "Layout:Model/Layer:CVC")
        assert cvc.text.endswith("\n\nGaine principale\nGaine secondaire")
        assert "Poteau" not in cvc.text and "TGBT" not in cvc.text
        rdc = _unit(res, "Layout:RDC/Layer:ELEC_CFO")
        assert "Tableau TGBT principal\nChemin de cable CF01" in rdc.text
        assert res.metadata["layouts"] == ["Model", "Layout1", "RDC", "R+1"]
        assert set(res.metadata["layers"]) >= {"0", "CVC", "ELEC_CFO", "STRUCTURE"}

    def test_off_and_frozen_layers_are_still_extracted(self):
        res = _x(B.build_layer_states())
        joined = "\n".join(_texts(res))
        assert "Texte calque eteint" in joined and "Texte calque gele" in joined
        assert "Layer state: off" in _unit(res, "Layout:Model/Layer:ETEINT").text
        assert "Layer state: frozen" in _unit(res, "Layout:Model/Layer:GELE").text
        assert res.metadata["layers_off"] == ["ETEINT"] and res.metadata["layers_frozen"] == ["GELE"]


# --------------------------------------------------------------------------- #
# DXF-3 — bloc + attributs
# --------------------------------------------------------------------------- #
class TestDxf3Block:
    def test_block_instance_unit_with_attributes_and_locator(self):
        res = _x(B.build_block_attribs())
        blk = _unit(res, "Layout:Model/Layer:CVC_EQUIPEMENTS/Block:CTA")
        assert blk.text == (
            "DXF block\nLayout: Model\nLayer: CVC_EQUIPEMENTS\nBlock: CTA\n"
            "REPERE: CTA-03\nDEBIT: 12000 m3/h\nPUISSANCE: 45 kW"
        )
        loc = blk.meta["locator"]
        assert loc["type"] == "dxf" and re.fullmatch(r"handle:[0-9A-F]+", loc["identifier"])
        # Le bloc sans attributs n'est PAS une unité, mais est compté.
        assert not any("Block:PORTE" in p for p in _paths(res))
        assert "INSERT: 2" in res.units[0].text
        assert "Blocks: CTA, PORTE" in res.units[0].text

    def test_block_definition_text_is_a_bounded_unit_without_attrib_duplication(self):
        res = _x(B.build_block_attribs())
        d = _unit(res, "Block:CTA")
        assert d.text == "DXF block definition\nBlock: CTA\nAttributes: REPERE, DEBIT, PUISSANCE\n\nCentrale de traitement d'air"
        joined = "\n".join(_texts(res))
        assert joined.count("CTA-03") == 1  # valeur ATTRIB une seule fois


# --------------------------------------------------------------------------- #
# DXF-4 — cotes
# --------------------------------------------------------------------------- #
class TestDxf4Dimension:
    def test_dimensions_are_semantic_lines_without_coordinates(self):
        res = _x(B.build_dimension())
        grp = _unit(res, "Layout:Model/Layer:COTES")
        assert "Dimension: 2500 mm" in grp.text
        assert "Dimension: 1200 mm (hors tout)" in grp.text
        assert "Dimension: VOIR DETAIL" in grp.text
        assert not COORDS.search(grp.text)
        assert "DIMENSION: 3" in res.units[0].text

    def test_unset_measurement_sentinel_falls_back_to_geometry(self):
        """Le code 42 est FACULTATIF : LibreDWG y écrit -1 quand la mesure n'est pas stockée.
        -1 n'est pas une longueur — la cote doit porter sa mesure géométrique réelle."""
        res = _x(B.build_dimension_unset_measurement())
        grp = _unit(res, "Layout:Model/Layer:COTES")
        assert "Dimension: 2100 mm" in grp.text
        assert "-1" not in grp.text


# --------------------------------------------------------------------------- #
# DXF-5 / 5B — XREF déclarées, jamais ouvertes
# --------------------------------------------------------------------------- #
class TestDxf5Xref:
    def test_declared_xref_is_listed_never_opened(self):
        res = _x(B.build_xref("architecte.dwg", "ARCHI"))
        assert res.metadata["xrefs_declared"] == [{"name": "ARCHI", "path": "architecte.dwg"}]
        assert "Declared XREF: ARCHI -> architecte.dwg" in res.units[0].text

    @pytest.mark.parametrize("path", ["../../../../etc/passwd", "file:///etc/passwd",
                                      "\\\\serveur\\partage\\plan.dwg", "https://example.invalid/plan.dwg"])
    def test_malicious_xref_paths_are_reported_not_resolved(self, path, monkeypatch):
        import builtins
        import socket

        opened = []
        real_open = builtins.open

        def spy_open(f, *a, **k):
            opened.append(str(f))
            return real_open(f, *a, **k)

        monkeypatch.setattr(builtins, "open", spy_open)
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("réseau")))
        res = _x(B.build_xref(path, "EXT"))
        assert res.metadata["xrefs_declared"][0]["path"] == path
        assert not any("passwd" in o or "plan.dwg" in o for o in opened)
        joined = "\n".join(_texts(res))
        assert "root:" not in joined


# --------------------------------------------------------------------------- #
# DXF-6 — malformé ; version ; DXF-14 — usurpations
# --------------------------------------------------------------------------- #
class TestDxf6Malformed:
    def test_truncated_is_malformed_not_repaired(self):
        data = B.build_basic()
        with pytest.raises(ExtractionError) as ei:
            _x(data[: len(data) // 2])
        assert ei.value.code == "dxf_malformed"

    def test_garbage_pairs_is_malformed(self):
        with pytest.raises(ExtractionError) as ei:
            _x(b"  0\nSECTION\n  2\nHEADER\nxx\n")
        assert ei.value.code == "dxf_malformed"

    def test_unsupported_version_mapping(self, monkeypatch):
        from apps.rag_core.services.ingest.extraction import dxf_extractor as D
        from ezdxf.lldxf.const import DXFVersionError

        def boom(*a, **k):
            raise DXFVersionError("version non supportée")

        monkeypatch.setattr(D, "_load_document", boom)
        with pytest.raises(ExtractionError) as ei:
            _x(B.build_basic())
        assert ei.value.code == "dxf_unsupported_version"

    def test_pdf_renamed_dxf_stays_pdf(self):
        assert _x(make_pdf_with_text(), name="renamed.dxf").metadata.get("format") == "pdf"

    def test_executable_renamed_dxf_is_refused(self):
        with pytest.raises(RejectedFile) as ei:
            _x(b"MZ\x90\x00" + b"\x00" * 60, name="evil.dxf")
        assert ei.value.code == "executable_refused"

    def test_plain_text_named_dxf_is_txt(self):
        assert _x(b"Compte rendu de chantier, section 2", name="notes.dxf").metadata.get("format") == "txt"


# --------------------------------------------------------------------------- #
# DXF-7 — complexité (cap entités)
# --------------------------------------------------------------------------- #
class TestDxf7Complexity:
    def test_default_cap(self):
        from apps.rag_core.services.ingest.extraction import config

        assert config.dxf_max_entities() == 250_000

    @override_settings(DOCUMENT_DXF_MAX_ENTITIES=20)
    def test_boundary(self):
        assert _x(B.build_entities(20)).metadata["entity_count"] == 20
        with pytest.raises(ExtractionComplexityExceeded) as ei:
            _x(B.build_entities(21))
        assert ei.value.code == "dxf_complexity_limit"
        assert ei.value.meta.get("dxf_entity_count") == 21


# --------------------------------------------------------------------------- #
# DXF-11 / 12 / 13 / 17 / 18 / 19
# --------------------------------------------------------------------------- #
class TestContent:
    def test_long_layer_is_one_unit_left_to_the_common_chunker(self):
        res = _x(B.build_long_layer(120))
        grp = _unit(res, "Layout:Model/Layer:ELEC_CFO")
        assert grp.text.count("Annotation electrique") == 120
        assert len(grp.text) > 4000
        assert len(res.units) == 2  # résumé + un groupe

    def test_geometry_noise_is_counted_not_indexed(self):
        res = _x(B.build_geometry_noise(3000))
        assert res.metadata["entity_counts"]["LINE"] == 3000
        assert "LINE: 3000" in res.units[0].text
        joined = "\n".join(_texts(res))
        assert "Local ventilation zorglub" in joined
        assert len(joined) < 2000
        assert not COORDS.search(joined)
        assert len(res.units) == 2

    def test_binary_dxf_is_supported_in_memory(self):
        data = B.build_binary()
        assert data.startswith(b"AutoCAD Binary DXF")
        res = _x(data)
        assert res.metadata["format"] == "dxf" and res.metadata["dxf_binary"] is True
        assert "CTA-BIN-07" in _unit(res, "Layout:Model/Layer:CVC").text

    def test_pre_r2007_codepage_is_decoded(self):
        res = _x(B.build_r2000_cp1252())
        assert res.metadata["dxf_version"] == "AC1015"
        assert "Débit réglé à 12000" in "\n".join(_texts(res))

    def test_mtext_formatting_codes_are_stripped(self):
        res = _x(B.build_mtext_formatted())
        grp = _unit(res, "Layout:Model/Layer:0")
        assert "Puissance 45 kW °" in grp.text
        assert "\\f" not in grp.text and "\\P" not in grp.text and "{" not in grp.text

    def test_mleader_text_is_extracted_when_available(self):
        res = _x(B.build_mleader())
        assert "Note repere ML-1" in "\n".join(_texts(res))

    def test_empty_drawing_is_a_controlled_empty_extraction(self):
        res = _x(B.build_empty())
        assert res.units == []
        assert res.status == ExtractionStatus.EMPTY

    def test_geometry_only_drawing_keeps_an_informative_summary(self):
        res = _x(B.build_geometry_only())
        assert len(res.units) == 1 and _paths(res) == ["Drawing"]
        assert "LINE: 1" in res.units[0].text and "TRAITS" in res.units[0].text

    def test_determinism(self):
        data = B.build_layouts()
        a, b = _x(data), _x(data)
        assert [(u.text, u.meta) for u in a.units] == [(u.text, u.meta) for u in b.units]
        assert a.metadata == b.metadata

    def test_metadata_lists_are_bounded(self):
        import ezdxf

        doc = B.new()
        for i in range(300):
            doc.layers.add(f"L{i:03d}")
        doc.modelspace().add_text("x")
        res = _x(B.to_bytes(doc))
        assert res.metadata["layers_count"] == 302
        assert len(res.metadata["layers"]) <= 200
        assert "dxf_layers_truncated" in res.warnings
        assert ezdxf.__version__ == res.metadata["parser_version"]
