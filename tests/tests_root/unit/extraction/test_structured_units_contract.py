"""contrat partagé d'extraction structurée : UNITÉS.

Ces tests sont PROPRIÉTAIRES du contrat générique ``ExtractionResult.units`` et de sa
voie d'indexation (``indexing._build_unit_specs``). Aucun format concret (XML, DXF,
IFC…) n'y figure : ils prouvent le socle que les lots suivants consommeront.

Les symboles introduits par 1A sont résolus PARESSEUSEMENT (``getattr``) pour que la
ligne de base (avant implémentation) échoue par ASSERTION et non par erreur d'import.
"""
from __future__ import annotations

import json

from apps.rag_core.services.ingest.extraction import base as B
from apps.rag_core.services.ingest.extraction.base import ExtractionResult, ExtractionStatus

LOCATOR = {"type": "element", "path": "/root/a[2]/b", "identifier": "id-42"}


def _unit(text: str, meta: dict | None = None):
    cls = getattr(B, "StructuredUnit", None)
    assert cls is not None, "StructuredUnit absent du contrat"
    return cls(text=text, meta=dict(meta or {}))


def _build_unit_specs(**kwargs):
    from apps.rag_core.services.ingest import indexing

    fn = getattr(indexing, "_build_unit_specs", None)
    assert fn is not None, "indexing._build_unit_specs absent"
    return fn(**kwargs)


# --------------------------------------------------------------------------- #
# U1 — les unités existent sur le résultat, sans rien casser d'existant
# --------------------------------------------------------------------------- #
class TestU1UnitsBaseline:
    def test_units_default_empty_and_backward_compatible(self):
        res = ExtractionResult(text="texte")
        assert hasattr(res, "units"), "ExtractionResult.units absent"
        assert res.units == []
        # Le contrat historique est intact : un résultat sans unités se comporte
        # exactement comme avant (texte seul, statut SUCCESS après finalize).
        res.finalize(fmt="txt", mime="text/plain", method="test")
        assert res.status == ExtractionStatus.SUCCESS
        assert res.is_empty() is False

    def test_typed_unit_carries_text_and_json_safe_meta(self):
        u = _unit("contenu", {"locator": LOCATOR, "depth": 2})
        assert u.text == "contenu"
        assert u.meta["locator"] == LOCATOR
        json.dumps(u.meta)  # JSON-safe par construction
        res = ExtractionResult(text="", units=[u])
        assert res.units[0] is u

    def test_units_only_result_is_not_empty(self):
        """Un résultat SANS texte global mais AVEC unités est exploitable : il ne doit
        pas être classé EMPTY (sinon l'indexation le refuserait ``empty_document``)."""
        res = ExtractionResult(text="", units=[_unit("Unité A", {"locator": LOCATOR})])
        assert res.is_empty() is False
        res.finalize(fmt="txt", mime="text/plain", method="test")
        assert res.status == ExtractionStatus.SUCCESS

    def test_blank_units_only_result_is_empty(self):
        res = ExtractionResult(text="", units=[_unit("   "), _unit("")])
        assert res.is_empty() is True
        res.finalize(fmt="txt", mime="text/plain", method="test")
        assert res.status == ExtractionStatus.EMPTY


# --------------------------------------------------------------------------- #
# U2 — une unité longue est fenêtrée par le chunker commun, sans page
# --------------------------------------------------------------------------- #
class TestU2LongUnitIsChunked:
    def test_long_unit_yields_several_specs_all_pageless_with_meta(self):
        long_text = ("Clause de chantier numéro un, dalle armée HA12 et coffrage bois. " * 40)
        res = ExtractionResult(text="", units=[_unit(long_text, {"locator": LOCATOR, "kind": "unit"})])
        specs = _build_unit_specs(result=res, filename="f.dat", chunk_size=300, overlap=30)
        assert len(specs) > 1
        assert [s.chunk_index for s in specs] == list(range(len(specs)))
        assert all(s.page_number is None for s in specs)
        assert all(s.meta.get("locator") == LOCATOR for s in specs)
        assert all(s.meta.get("kind") == "unit" for s in specs)
        assert all(s.text.strip() for s in specs)
        # Le fenêtrage suit la politique commune : aucun chunk ne dépasse la taille.
        assert all(len(s.text) <= 300 for s in specs)

    def test_short_unit_yields_exactly_one_spec(self):
        res = ExtractionResult(text="", units=[_unit("Courte unité.", {"locator": LOCATOR})])
        specs = _build_unit_specs(result=res, filename="f.dat", chunk_size=300, overlap=30)
        assert len(specs) == 1
        assert specs[0].text == "Courte unité."
        assert specs[0].page_number is None
        assert specs[0].meta["locator"] == LOCATOR


# --------------------------------------------------------------------------- #
# U4 — plusieurs unités : ordre déterministe, méta isolée par unité
# --------------------------------------------------------------------------- #
class TestU4MultipleUnitsOrderAndIsolation:
    def test_order_follows_units_and_meta_never_leaks_between_units(self):
        u1 = _unit("Première unité.", {"locator": {"path": "/a[1]"}, "only_on": "one"})
        u2 = _unit("Deuxième unité.", {"locator": {"path": "/a[2]"}})
        u3 = _unit("Troisième unité.", {"locator": {"path": "/a[3]"}, "only_on": "three"})
        res = ExtractionResult(text="", units=[u1, u2, u3])
        specs = _build_unit_specs(result=res, filename="f.dat", chunk_size=300, overlap=30)
        assert [s.text for s in specs] == ["Première unité.", "Deuxième unité.", "Troisième unité."]
        assert [s.chunk_index for s in specs] == [0, 1, 2]
        assert [s.meta["locator"]["path"] for s in specs] == ["/a[1]", "/a[2]", "/a[3]"]
        assert specs[0].meta.get("only_on") == "one"
        assert "only_on" not in specs[1].meta
        assert specs[2].meta.get("only_on") == "three"
        # Isolation : muter la méta d'un chunk n'affecte ni l'unité ni un autre chunk.
        specs[0].meta["locator"]["path"] = "/muté"
        assert u1.meta["locator"]["path"] == "/a[1]"
        assert specs[1].meta["locator"]["path"] == "/a[2]"

    def test_two_runs_are_byte_identical(self):
        units = [_unit(f"Unité {i} " * 30, {"locator": {"path": f"/u[{i}]"}}) for i in range(5)]
        res = ExtractionResult(text="", units=units)
        a = _build_unit_specs(result=res, filename="f.dat", chunk_size=120, overlap=12)
        b = _build_unit_specs(result=res, filename="f.dat", chunk_size=120, overlap=12)
        assert [(s.chunk_index, s.text, s.char_start, s.char_end, s.meta) for s in a] == \
               [(s.chunk_index, s.text, s.char_start, s.char_end, s.meta) for s in b]


# --------------------------------------------------------------------------- #
# U5 — unité vide / blanche : jamais un chunk vide, indices contigus
# --------------------------------------------------------------------------- #
class TestU5EmptyUnits:
    def test_blank_units_are_skipped_and_indices_stay_contiguous(self):
        from apps.rag_core.services.ingest.utils import filter_nonblank_specs

        res = ExtractionResult(text="", units=[
            _unit("Avant.", {"locator": {"path": "/1"}}),
            _unit("", {"locator": {"path": "/2"}}),
            _unit("   \n\t ", {"locator": {"path": "/3"}}),
            _unit("Après.", {"locator": {"path": "/4"}}),
        ])
        specs = _build_unit_specs(result=res, filename="f.dat", chunk_size=300, overlap=30)
        assert [s.text for s in specs] == ["Avant.", "Après."]
        assert [s.chunk_index for s in specs] == [0, 1]
        assert [s.meta["locator"]["path"] for s in specs] == ["/1", "/4"]
        assert filter_nonblank_specs(specs) == specs

    def test_all_blank_units_yield_no_spec(self):
        res = ExtractionResult(text="", units=[_unit(""), _unit("  ")])
        assert _build_unit_specs(result=res, filename="f.dat", chunk_size=300, overlap=30) == []
