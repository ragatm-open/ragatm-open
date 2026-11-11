"""NATIVE-TABLE-RECOVERY-2 — bandeaux d'en-tête réglés, continuation sans en-tête répété,
texte résiduel fidèle.

Constats réels reproduits (NATIVE-TABLE-GUARD-1R, extrait d'un dossier de consultation réel, tableau paginé) :

1. un tableau paginé n'est titré que sur sa première page, dans un bandeau réglé que
   l'analyseur ne reconnaît pas comme tableau ; les pages suivantes portaient des colonnes
   neutres « Column N » et la quantité demandée ne se rattachait plus à « QTE » ;
2. un cartouche réglé en tête de chaque page s'intercalait entre les morceaux du corps et
   rompait la chaîne ;
3. structurer ce cartouche faisait REMPLACER le texte de la page par une reconstruction
   ``pdfplumber`` découpée lettre à lettre (glyphes non droits) : deux lignes recherchées
   disparaissaient de l'index.

Chaque refus est vérifié autant que chaque rattachement : un bandeau n'est jamais attribué à
un corps qu'il ne nomme pas géométriquement.
"""
from __future__ import annotations

import pytest

# RAGatm Open : moteur natif PyMuPDF = extra optionnel `pdf-mupdf` (AGPL), jamais installé par défaut.
fitz = pytest.importorskip("fitz")

from apps.rag_core.services.ingest.extraction import pdf_tables as P

from . import _pdf_header_band_fixtures as F

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")

NEUTRAL = [P.NEUTRAL_COLUMN.format(i + 1) for i in range(5)]


def _five_column_tables(data: bytes):
    """Tableaux de l'analyseur (grilles complètes) ; les grilles à rangées non tracées certifiées par
    GROUNDING-3 (`structure` renseignée) sont testées à part."""
    return [t for t in P.extract_generic_tables(data).tables if len(t.schema) == 5 and t.structure is None]


def _only_table(data: bytes) -> P.LogicalTable:
    tables = _five_column_tables(data)
    assert len(tables) == 1, [t.schema for t in tables]
    return tables[0]


def _row(table: P.LogicalTable, label: str):
    for r in table.rows:
        for f in r.fragments:
            if label in f.cells.values():
                return r, f
    raise AssertionError(f"ligne {label!r} absente")


# --------------------------------------------------------------------------- #
# Rattachement prouvé
# --------------------------------------------------------------------------- #
class TestReattachment:
    def test_separate_band_names_the_body_below(self):
        rep = P.extract_generic_tables(F.band_same_page())
        table = _only_table(F.band_same_page())
        assert table.schema == F.SCHEMA
        assert table.header_source["relation"] == "same_page"
        # Le bandeau reconnu comme tableau appartient au corps : structuré AVEC lui,
        # jamais publié seul ni compté comme grille non structurée.
        assert {(o["outcome"], o["reason"]) for o in rep.outcomes} == {
            (P.OUTCOME_STRUCTURED, ""), (P.OUTCOME_STRUCTURED, "header_band")}
        assert not rep.warnings

    def test_value_lands_under_its_named_column_with_provenance(self):
        table = _only_table(F.band_same_page())
        row, frag = _row(table, "Item 7")
        assert frag.cells == {"ID": "7", "Description": "Item 7", "Qty": "10,5", "Unit": "m", "Ref": "4707"}
        meta = P.fragment_to_meta(table, row, frag)
        assert meta["column_schema"] == F.SCHEMA
        assert meta["source_page"] == 1 and meta["logical_row_id"] and meta["table_id"] == table.table_id
        assert set(meta["cell_boxes"]) == set(frag.cells)
        x0, top, x1, bottom = meta["cell_boxes"]["Qty"]
        assert F.EDGES[2] - 1 <= x0 < x1 <= F.EDGES[3] + 1 and top < bottom
        assert meta["header_source"]["kind"] == "ruled_header_band"
        text = P.fragment_to_text(table, row, frag)
        assert "Qty:\n10,5" in text and "Column" not in text

    def test_small_ruling_offset_within_tolerance(self):
        assert _only_table(F.band_same_page(shift=1.5)).schema == F.SCHEMA

    def test_hierarchical_band_keeps_parent_and_leaves(self):
        table = _only_table(F.band_same_page(
            labels=[["ID", "Description", "Measure", "", "Ref"], ["", "", "Qty", "Unit", ""]],
            spans=[(0, 2, 3)]))
        assert table.column_paths == [["ID"], ["Description"], ["Measure", "Qty"], ["Measure", "Unit"], ["Ref"]]

    def test_duplicate_parent_labels_are_distinguished_by_leaves(self):
        """Deux colonnes sous le même parent : les deux valeurs restent, jamais écrasées."""
        table = _only_table(F.band_same_page(
            labels=[["ID", "Description", "Level", "", "Ref"], ["", "", "Low", "High", ""]],
            spans=[(0, 2, 3)]))
        assert table.schema[2:4] == ["Level > Low", "Level > High"]
        _row_, frag = _row(table, "Item 3")
        assert frag.cells["Level > Low"] == "4,5" and frag.cells["Level > High"] == "m"

    def test_row_groups_are_kept_as_context(self):
        table = _only_table(F.band_same_page(separators=(3, 7)))
        assert table.schema == F.SCHEMA
        row, _ = _row(table, "Item 4")
        assert row.context_label == "Group 3"

    def test_band_on_previous_page_names_a_paginated_body_despite_page_furniture(self):
        tables = _five_column_tables(F.band_previous_page())
        assert len(tables) == 1
        table = tables[0]
        # Les deux morceaux du corps forment UN tableau malgré le cartouche intercalé.
        assert table.source_pages == [2, 3] and table.schema == F.SCHEMA
        assert table.header_source == {"kind": "ruled_header_band", "relation": "previous_page",
                                       "page": 1, "bbox": table.header_source["bbox"]}
        _r, frag = _row(table, "Item 50")
        assert frag.page_number == 3 and frag.cells["Ref"] == "4750"


# --------------------------------------------------------------------------- #
# Refus : jamais de bandeau attribué sans preuve géométrique
# --------------------------------------------------------------------------- #
class TestFailClosed:
    @pytest.mark.parametrize("data", [
        pytest.param(lambda: F.band_same_page(shift=6.0), id="rulings-misaligned"),
        pytest.param(lambda: F.band_same_page(gap=40.0), id="band-too-far-above"),
        pytest.param(lambda: F.band_same_page(labels=[["ID", "Description", "Value", "Value", "Ref"]]),
                     id="duplicate-flat-labels"),
        pytest.param(lambda: F.band_same_page(labels=[["ID", "Description", "2026", "Unit", "Ref"]]),
                     id="numeric-band-cell"),
        pytest.param(lambda: F.band_same_page(labels=[["ID", "Description", "-", "Unit", "Ref"]]),
                     id="letterless-band-cell"),
        pytest.param(lambda: F.band_same_page(band_edges=[40.0, 70.0, 250.0, 380.0, 460.0],
                                              labels=[["ID", "Description", "Qty Unit", "Ref", ""]]),
                     id="merged-band-columns"),
        pytest.param(lambda: F.band_previous_page(grid_reaches_bottom=False), id="previous-grid-ends-early"),
        pytest.param(F.body_only, id="body-without-band"),
    ])
    def test_body_keeps_neutral_columns(self, data):
        table = _only_table(data())
        assert table.schema == NEUTRAL and table.header_source is None

    def test_independent_table_below_does_not_inherit_the_band(self):
        tables = _five_column_tables(F.adjacent_tables())
        named = [t for t in tables if t.header_source is not None]
        other = [t for t in tables if t.header_source is None]
        assert len(named) == 1 and len(other) == 1
        named, other = named[0], other[0]
        assert named.schema == F.SCHEMA
        assert other.schema == NEUTRAL and other.header_source is None
        assert _row(other, "Item 50")[1].cells["Column 5"] == "4750"

    def test_band_without_body_is_not_a_table(self):
        rep = P.extract_generic_tables(F.band_only())
        assert not rep.tables
        assert [o["outcome"] for o in rep.outcomes] == [P.OUTCOME_FALLBACK]

    def test_misaligned_previous_page_body_is_not_chained_nor_named(self):
        shifted = [e + 6.0 for e in F.EDGES]
        tables = _five_column_tables(F.band_previous_page(edges_page2=shifted))
        assert all(t.header_source is None or t.source_pages == [3] for t in tables)
        assert not any(t.source_pages == [2, 3] for t in tables)


# --------------------------------------------------------------------------- #
# Texte résiduel : jamais reconstruit depuis des mots mal découpés
# --------------------------------------------------------------------------- #
class TestResidualText:
    def test_skewed_glyphs_page_keeps_its_words(self):
        # Grille de la page 1 rendue NON certifiable : son texte reste dans la page.
        rep = P.extract_generic_tables(F.band_previous_page(skew=True, straddle=True))
        assert rep.residual_engine[1] == "pymupdf"
        text = rep.residual_page_text[1]
        assert "Gizmo beta" in text and "4811" in text
        assert "Project" not in text  # le cartouche structuré reste retiré

    def test_upright_glyphs_keep_the_existing_engine(self):
        rep = P.extract_generic_tables(F.band_previous_page(straddle=True))
        assert set(rep.residual_engine.values()) == {"pdfplumber"}
        assert "Gizmo beta" in rep.residual_page_text[1]

    def test_split_words_fall_back_to_native_words(self):
        native = [{"x0": 10, "x1": 40, "top": 100, "bottom": 108, "text": "Gizmo"},
                  {"x0": 42, "x1": 60, "top": 100, "bottom": 108, "text": "beta"},
                  {"x0": 10, "x1": 40, "top": 10, "bottom": 18, "text": "Header"}]
        split = [{"x0": 10 + i * 4, "x1": 13 + i * 4, "top": 100, "bottom": 108, "text": ch}
                 for i, ch in enumerate("Gizmobeta")]
        rep = P.TableExtractionReport()
        P._residual_text(rep, 1, [(0, 5, 100, 20)], split, native)
        assert (rep.residual_engine[1], rep.residual_page_text[1]) == ("pymupdf", "Gizmo beta")

    def test_without_native_reference_behaviour_is_unchanged(self):
        words = [{"x0": 10, "x1": 40, "top": 100, "bottom": 108, "text": "Gizmo"}]
        rep = P.TableExtractionReport()
        P._residual_text(rep, 1, [], words, None)
        assert (rep.residual_engine[1], rep.residual_page_text[1]) == ("pdfplumber", "Gizmo")
