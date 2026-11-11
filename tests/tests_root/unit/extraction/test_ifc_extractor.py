"""extracteur IFC : sémantique BIM, sans géométrie.

IFC-1/2/3 schémas (IFC-1 propriétaire, xfail strict sur la base), IFC-4 hiérarchie,
IFC-5 psets, IFC-6 quantités, IFC-7 matériaux, IFC-8 systèmes, IFC-9 classification,
éléments pauvres groupés, élément long, orphelin, références externes, malformé,
schéma non supporté, complexité (pré-scan + modèle), preuve statique « aucune
géométrie », déterminisme, bornes de métadonnées.
"""
from __future__ import annotations

import re

import pytest
from apps.rag_core.services.ingest.extraction import (
    ExtractionComplexityExceeded,
    ExtractionError,
    ExtractionStatus,
    extract_document,
)
from django.test import override_settings

from . import _ifc_builders as B

pytestmark = pytest.mark.usefixtures("_ifc_on")


@pytest.fixture
def _ifc_on():
    with override_settings(DOCUMENT_IFC_ENABLED=True):
        yield


def _x(data: bytes, name="model.ifc"):
    return extract_document(data, filename=name, content_type="application/x-step")


def _texts(res):
    return [u.text for u in res.units]


def _paths(res):
    return [u.meta["locator"]["path"] for u in res.units]


def _unit(res, needle):
    return next(u for u in res.units if needle in u.meta["locator"]["path"])


DOOR_PATH = "Centre hospitalier / Site principal / Building A / Level 02 / Local technique 2.14 / IfcDoor / D-201"
GEOMETRY = re.compile(r"IFCCARTESIANPOINT|IFCDIRECTION|IFCPOLYLOOP|IFCFACE|#\d+=", re.I)


# --------------------------------------------------------------------------- #
# IFC-1/2/3 — schémas (IFC-1 = contrat propriétaire)
# --------------------------------------------------------------------------- #
class TestIfc1Schemas:
    def test_ifc4_model_becomes_structured_units(self):
        res = _x(B.build_full("IFC4"))
        assert res.metadata.get("format") == "ifc", "IFC traité comme texte brut"
        assert res.status == ExtractionStatus.SUCCESS and res.text == ""
        assert res.units
        assert res.metadata["schema"] == "IFC4" and res.metadata["parser"] == "ifcopenshell"
        assert res.metadata["parser_version"] and res.metadata["project_name"] == "Centre hospitalier"
        assert all(u.meta["locator"]["type"] == "ifc" for u in res.units)
        assert all(u.meta.get("structured_format") == "ifc" for u in res.units)
        joined = "\n".join(_texts(res))
        assert "D-201" in joined and "EI60" in joined
        assert not GEOMETRY.search(joined)

    @pytest.mark.parametrize("schema,expected_identifier", [("IFC2X3", "IFC2X3"), ("IFC4X3", "IFC4X3_ADD2")])
    def test_other_schemas_parse_with_exact_schema_metadata(self, schema, expected_identifier):
        res = _x(B.build_full(schema))
        assert res.metadata["schema"] == schema
        assert res.metadata["schema_identifier"] == expected_identifier
        door = _unit(res, "IfcDoor / D-201")
        assert "Pset_DoorCommon.FireRating: EI60" in door.text
        assert door.meta["locator"]["identifier"] == f"GlobalId:{B.GID_DOOR}"


# --------------------------------------------------------------------------- #
# IFC-4..9 — hiérarchie, psets, quantités, matériaux, systèmes, classification
# --------------------------------------------------------------------------- #
class TestSemantics:
    def test_rich_element_unit(self):
        res = _x(B.build_full("IFC4"))
        door = _unit(res, "IfcDoor / D-201")
        assert door.meta["locator"] == {"type": "ifc", "path": DOOR_PATH, "identifier": f"GlobalId:{B.GID_DOOR}"}
        t = door.text
        assert t.startswith("IFC element\nPath: Centre hospitalier / Site principal / Building A / Level 02 / Local technique 2.14\n")
        for line in ("Class: IfcDoor", f"GlobalId: {B.GID_DOOR}", "Name: D-201",
                     "Description: Porte coupe-feu du local technique", "Tag: PORTE-201", "PredefinedType: DOOR",
                     "Type: Porte EI60 900x2100", "Pset_DoorCommon.FireRating: EI60", "Pset_DoorCommon.IsExternal: false",
                     "Qto_DoorBaseQuantities.Height: 2100 mm", "Qto_DoorBaseQuantities.Width: 900 mm",
                     "Qto_DoorBaseQuantities.Area: 1.89 m2", "Material: Acier",
                     "Classification: Uniclass 2015 Pr_30_59_98 Doors", "System: VENTILATION-CTA03"):
            assert line in t, line
        assert ".id:" not in t and "\nid:" not in t

    def test_hierarchy_and_spatial_units(self):
        res = _x(B.build_full("IFC4"))
        paths = _paths(res)
        assert paths[0] == "Model"
        assert "Centre hospitalier / Site principal" in paths
        assert "Centre hospitalier / Site principal / Building A" in paths
        assert "Centre hospitalier / Site principal / Building A / Level 02" in paths
        assert "Centre hospitalier / Site principal / Building A / Level 02 / Local technique 2.14" in paths
        storey = _unit(res, "Level 02")
        assert storey.text.startswith("IFC storey\n")
        assert "Spaces: 1" in storey.text and "IfcWall: 3" in storey.text
        assert storey.meta["locator"]["identifier"] == f"GlobalId:{B.GID_STOREY}"
        space = _unit(res, "Local technique 2.14")
        assert "LongName: Local technique 2.14" in space.text and "IfcDoor: 1" in space.text
        # ordre : résumé, hiérarchie (site, bâtiment, niveau, espace), puis éléments
        assert paths.index("Centre hospitalier / Site principal") < paths.index(DOOR_PATH)

    def test_poor_elements_are_grouped_not_listed(self):
        res = _x(B.build_poor_elements(12))
        groups = [u for u in res.units if u.text.startswith("IFC element group\n")]
        assert len(groups) == 1
        g = groups[0]
        assert g.text == ("IFC element group\nPath: Centre hospitalier / Site principal / Building A / Level 02\n"
                          "Class: IfcWall\nType: Mur beton 20 cm\nCount: 12")
        assert "2Wabc" not in "\n".join(_texts(res))
        assert res.metadata["grouped_element_units"] == 1 and res.metadata["rich_element_units"] == 0
        assert res.metadata["product_count"] == 12 + 4  # + site, bâtiment, niveau, espace

    def test_type_summary_unit_and_instance_reference(self):
        res = _x(B.build_poor_elements(12))
        t = _unit(res, "IfcWallType / Mur beton 20 cm")
        assert t.text.startswith("IFC type\nClass: IfcWallType\nName: Mur beton 20 cm\nInstances: 12")

    def test_distribution_system_on_terminal(self):
        res = _x(B.build_full("IFC4"))
        term = _unit(res, "IfcFlowTerminal / CTA-03")
        assert "System: CVC-EXTRACTION" in term.text

    def test_uncontained_element_gets_a_deterministic_fallback_path(self):
        res = _x(B.build_uncontained())
        door = _unit(res, "IfcDoor / D-ORPHAN")
        assert door.meta["locator"]["path"] == "Centre hospitalier / Uncontained / IfcDoor / D-ORPHAN"
        assert "Porte hors hiérarchie" in door.text

    def test_summary_unit(self):
        res = _x(B.build_full("IFC4"))
        s = res.units[0].text
        assert s.startswith("IFC model\nSchema: IFC4\nProject: Centre hospitalier\n")
        assert "Sites: 1" in s and "Buildings: 1" in s and "Storeys: 1" in s and "Spaces: 1" in s
        assert "IfcWall: 3" in s and "IfcDoor: 1" in s
        assert not re.search(r"GlobalId|#\d+", s)


# --------------------------------------------------------------------------- #
# Références externes, sécurité, erreurs, complexité
# --------------------------------------------------------------------------- #
class TestSecurityAndErrors:
    def test_external_references_are_metadata_only_never_resolved(self, monkeypatch):
        import builtins
        import socket

        opened = []
        real_open = builtins.open

        def spy(f, *a, **k):
            opened.append(str(f))
            return real_open(f, *a, **k)

        monkeypatch.setattr(builtins, "open", spy)
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("réseau")))
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dns")))
        res = _x(B.build_external_refs())
        assert res.metadata["external_references_count"] >= 2
        assert not any("passwd" in o or "example.invalid" in o for o in opened)
        joined = "\n".join(_texts(res))
        assert "root:" not in joined

    def test_no_geometry_import_in_production_code(self):
        from pathlib import Path

        from apps.rag_core.services.ingest.extraction import ifc_extractor as X

        src = Path(X.__file__).read_text(encoding="utf-8")
        for forbidden in ("ifcopenshell.geom", "create_shape", "geom.settings", "OCC", "tempfile", "subprocess"):
            assert forbidden not in src, forbidden

    def test_malformed_is_controlled(self):
        with pytest.raises(ExtractionError) as ei:
            _x(b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\n#1=IFCPROJECT(\nENDSEC;\n")
        assert ei.value.code == "ifc_malformed"
        data = B.build_full("IFC4")
        with pytest.raises(ExtractionError) as ei:
            _x(data[: len(data) // 2])
        assert ei.value.code == "ifc_malformed"

    def test_error_path_produces_no_unraisable_noise(self, monkeypatch):
        """§29 : la finalisation bruyante du wrapper natif (KeyError dans __del__) est
        contenue pendant l'extraction — rien ne remonte au crochet du processus."""
        import gc
        import sys

        seen = []
        monkeypatch.setattr(sys, "unraisablehook", lambda u: seen.append(repr(getattr(u, "object", None))))
        bad = (b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(x);\nFILE_NAME('','',(''),(''),'','','');\n"
               b"FILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\n#1=IFCPROJECT('2Pabcdefghijklmnopqr0P',$,'P',$,$,$,$,$,$);\n"
               b"ENDSEC;\nEND-ISO-10303-21;\n")
        for _ in range(3):
            with pytest.raises(ExtractionError) as ei:
                _x(bad)
            assert ei.value.code == "ifc_malformed"
        _x(B.build_full("IFC4"))
        gc.collect()
        assert not [x for x in seen if "ifcopenshell" in x], seen

    def test_unsupported_schema_is_controlled(self):
        with pytest.raises(ExtractionError) as ei:
            _x(B.build_header_only("IFC9X9"))
        assert ei.value.code == "ifc_unsupported_schema"
        with pytest.raises(ExtractionError) as ei:
            _x(B.build_header_only("AUTOMOTIVE_DESIGN"))
        assert ei.value.code == "ifc_unsupported_schema"

    def test_default_cap(self):
        from apps.rag_core.services.ingest.extraction import config

        assert config.ifc_max_entities() == 250_000

    @override_settings(DOCUMENT_IFC_MAX_ENTITIES=30)
    def test_prescan_boundary_aborts_before_loading_the_model(self, monkeypatch):
        from apps.rag_core.services.ingest.extraction import ifc_extractor as X

        assert _x(B.build_statements(30)).metadata["entity_count"] == 30
        calls = {"load": 0}
        real = X._load_model

        def spy(text):
            calls["load"] += 1
            return real(text)

        monkeypatch.setattr(X, "_load_model", spy)
        with pytest.raises(ExtractionComplexityExceeded) as ei:
            _x(B.build_statements(31))
        assert ei.value.code == "ifc_complexity_limit"
        assert ei.value.meta.get("ifc_prescan_count") == 31
        assert calls["load"] == 0, "le modèle ne doit pas être chargé après le pré-scan"

    @override_settings(DOCUMENT_IFC_MAX_ENTITIES=40)
    def test_model_count_boundary(self, monkeypatch):
        """Le pré-scan est borné et structurel ; le compte définitif vient du modèle."""
        from apps.rag_core.services.ingest.extraction import ifc_extractor as X

        monkeypatch.setattr(X, "_prescan_statement_count", lambda data, cap: 0)  # neutralise le pré-scan
        data = B.build_full("IFC4")  # ~66 entités > 40
        with pytest.raises(ExtractionComplexityExceeded) as ei:
            _x(data)
        assert ei.value.code == "ifc_complexity_limit" and ei.value.meta.get("ifc_entity_count", 0) > 40

    def test_prescan_ignores_hash_inside_strings_and_comments(self):
        from apps.rag_core.services.ingest.extraction import ifc_extractor as X

        data = (b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\n"
                b"#1=IFCPROPERTYSINGLEVALUE('a #2= b',$,IFCTEXT('x #3=y'),$);\n/* #4= commentaire */\n"
                b"#2=IFCPROPERTYSINGLEVALUE('c',$,$,$);\nENDSEC;\nEND-ISO-10303-21;\n")
        assert X._prescan_statement_count(data, 100) == 2


# --------------------------------------------------------------------------- #
# Élément long, vide, déterminisme, bornes
# --------------------------------------------------------------------------- #
class TestContent:
    def test_long_element_is_one_unit_left_to_the_common_chunker(self):
        res = _x(B.build_long_element(120))
        door = _unit(res, "IfcDoor / D-LONG")
        assert door.text.count("Pset_Custom.Propriete_") == 120
        assert len(door.text) > 5000

    def test_empty_model_is_a_controlled_empty_extraction(self):
        res = _x(B.build_empty())
        assert res.units == [] and res.status == ExtractionStatus.EMPTY

    def test_determinism(self):
        data = B.build_full("IFC4")
        a, b = _x(data), _x(data)
        assert [(u.text, u.meta) for u in a.units] == [(u.text, u.meta) for u in b.units]
        assert a.metadata == b.metadata

    def test_metadata_counts(self):
        res = _x(B.build_full("IFC4"))
        m = res.metadata
        assert m["sites_count"] == 1 and m["buildings_count"] == 1 and m["storeys_count"] == 1 and m["spaces_count"] == 1
        assert m["entity_count"] > m["product_count"] >= 9
        assert m["rich_element_units"] == 2  # porte + terminal
        assert m["grouped_element_units"] == 1  # 3 murs
        assert m["spatial_summary_units"] == 4
        assert m["semantic_unit_count"] == len(res.units)
        assert isinstance(m["element_class_counts"], dict) and m["element_class_counts"]["IfcWall"] == 3

    def test_huge_property_value_is_bounded_with_warning(self):
        f = B.new("IFC4")
        _, run, _ = B._api()
        proj, site, bld, sto, spc = B._hierarchy(f, run)
        door = run("root.create_entity", f, ifc_class="IfcDoor", name="D-BIG")
        door.GlobalId = B.GID_DOOR
        run("spatial.assign_container", f, relating_structure=spc, products=[door])
        pset = run("pset.add_pset", f, product=door, name="Pset_Big")
        run("pset.edit_pset", f, pset=pset, properties={"Texte": "x" * 50_000})
        res = _x(B.to_bytes(f))
        door_unit = _unit(res, "IfcDoor / D-BIG")
        assert len(door_unit.text) < 10_000
        assert "ifc_value_truncated" in res.warnings
