"""contrat d'évidence du candidat (sans base de données).

`visual_units` est un rail ADDITIF, distinct de `units` (prioritaire et exclusif).
L'évaluation d'indexabilité doit compter TOUTE l'évidence : texte, unités
structurées, fragments tabulaires porteurs d'une valeur lue, unités visuelles.
"""
from __future__ import annotations

from apps.rag_core.services.ingest import indexing
from apps.rag_core.services.ingest.extraction.base import ExtractionResult, StructuredUnit


def _generic_table(columns_present, text="Row: r\n\nA:\n1"):
    return {"markdown": text, "meta": {"kind": "table", "table_type": "generic",
                                       "columns_present": list(columns_present)}}


class TestVisualUnitsField:
    def test_field_exists_and_is_empty_by_default(self):
        res = ExtractionResult()
        assert res.visual_units == []

    def test_independent_from_units(self):
        res = ExtractionResult()
        res.visual_units.append(StructuredUnit(text="Visual type: photograph", meta={}))
        assert res.units == []

    def test_default_not_shared(self):
        assert ExtractionResult().visual_units is not ExtractionResult().visual_units


class TestHasIndexableContent:
    def test_text_still_counts(self):
        assert indexing._has_indexable_content(ExtractionResult(text="hello")) is True

    def test_units_still_count(self):
        res = ExtractionResult(units=[StructuredUnit(text="u")])
        assert indexing._has_indexable_content(res) is True

    def test_nothing_is_not_indexable(self):
        assert indexing._has_indexable_content(ExtractionResult()) is False

    def test_table_only_with_filled_value(self):
        res = ExtractionResult(tables=[_generic_table(["A"])])
        assert indexing._has_indexable_content(res) is True

    def test_atomic_table_only(self):
        res = ExtractionResult(tables=[{"markdown": "| a | b |", "meta": {"table_type": "pile"}}])
        assert indexing._has_indexable_content(res) is True

    def test_generic_table_without_any_read_value_is_not_evidence(self):
        res = ExtractionResult(tables=[_generic_table([], text="Row: r\n\nA:\n[unreadable]")])
        assert indexing._has_indexable_content(res) is False

    def test_visual_unit_only(self):
        res = ExtractionResult(visual_units=[StructuredUnit(text="Visual type: photograph")])
        assert indexing._has_indexable_content(res) is True

    def test_blank_visual_unit_is_not_evidence(self):
        res = ExtractionResult(visual_units=[StructuredUnit(text="   ")])
        assert indexing._has_indexable_content(res) is False

    def test_status_empty_is_recomputed_for_visual_evidence(self):
        res = ExtractionResult(visual_units=[StructuredUnit(text="Visual type: diagram")])
        res.finalize(fmt="image", mime="image/png", method="image:ocr")
        assert res.status.value != "empty"


class TestSpecBuilders:
    def test_visual_unit_specs_have_no_page_and_copy_meta(self):
        meta = {"visual": {"kind": "photo", "asset_hash": "a" * 64}, "locator": {"type": "image"}}
        res = ExtractionResult(visual_units=[StructuredUnit(text="Visual type: photograph\nX", meta=meta)])
        specs = indexing._build_visual_unit_specs(result=res, filename="p.png", chunk_size=300,
                                                  overlap=50, start_chunk_index=7)
        assert [s.chunk_index for s in specs] == [7]
        assert specs[0].page_number is None
        assert specs[0].meta["visual"]["asset_hash"] == "a" * 64
        specs[0].meta["visual"]["kind"] = "mutated"
        assert meta["visual"]["kind"] == "photo"

    def test_empty_visual_units_is_a_noop(self):
        assert indexing._build_visual_unit_specs(result=ExtractionResult(), filename="p.png",
                                                 chunk_size=300, overlap=50,
                                                 start_chunk_index=0) == []

    def test_blank_visual_unit_consumes_no_index(self):
        res = ExtractionResult(visual_units=[StructuredUnit(text=" "), StructuredUnit(text="Y")])
        specs = indexing._build_visual_unit_specs(result=res, filename="p.png", chunk_size=300,
                                                  overlap=50, start_chunk_index=0)
        assert [s.chunk_index for s in specs] == [0]
