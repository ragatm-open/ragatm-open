"""NATIVE-TABLE-GROUNDING-3 — certification des grilles à colonnes réglées sans filets horizontaux.

Constat réel (page de tableau de prix d'un dossier de consultation) : colonnes tracées, rangées non tracées, cellules multilignes
centrées sur leur ligne d'ancrage ; l'analyseur n'y reconnaissait aucun tableau et les valeurs
restaient du texte non probant. Une rangée n'est certifiée que si sa structure est démontrée ;
chaque refus est vérifié autant que chaque certification.
"""
from __future__ import annotations

import pytest

# RAGatm Open : moteur natif PyMuPDF = extra optionnel `pdf-mupdf` (AGPL), jamais installé par défaut.
fitz = pytest.importorskip("fitz")

from apps.rag_core.services.ingest.extraction import pdf_tables as P

from . import _pdf_header_band_fixtures as B
from . import _pdf_ruleless_fixtures as F

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")


def _grids(data: bytes):
    rep = P.extract_generic_tables(data)
    return rep, [t for t in rep.tables if t.structure == P.STRUCTURE_RULED_COLUMNS_ANCHOR_ROWS]


def _data_rows(table):
    return [r for r in table.rows if r.row_kind == P.ROW_KIND_DATA]


def _cells(table, label):
    return next(r.fragments[0] for r in _data_rows(table) if r.fragments[0].cells.get("Description") == label)


class TestCertification:
    def test_grid_without_horizontal_rules_is_certified_cell_by_cell(self):
        rep, [table] = _grids(F.grid(F.numeric_rows(6), header=[B.SCHEMA]))
        assert table.schema == B.SCHEMA and len(_data_rows(table)) == 6
        frag = _cells(table, "Item 4")
        assert frag.cells == {"ID": "4", "Description": "Item 4", "Qty": "6,0", "Unit": "m", "Ref": "4704"}
        # Provenance géométrique : chaque valeur dans SA colonne, sur la page.
        for i, label in enumerate(B.SCHEMA):
            x0, top, x1, bottom = frag.cell_boxes[label]
            assert B.EDGES[i] - 1 <= x0 < x1 <= B.EDGES[i + 1] + 1 and top < bottom
        meta = P.fragment_to_meta(table, _data_rows(table)[3], frag)
        assert meta["structure"] == P.STRUCTURE_RULED_COLUMNS_ANCHOR_ROWS
        assert meta["header_source"]["relation"] == "same_grid" and meta["source_page"] == 1
        out = next(o for o in rep.outcomes if o.get("reason") == "ruleless_grid")
        assert (out["rows_certified"], out["rows_refused"]) == (6, 0)
        # Seules les rangées CERTIFIÉES quittent le texte de page (l'en-tête y reste).
        assert "Item 4" not in rep.residual_page_text[1] and "Description" in rep.residual_page_text[1]

    def test_multiline_cells_centered_on_their_anchor_form_one_row(self):
        _rep, [table] = _grids(F.grid(F.multiline_rows(), header=[B.SCHEMA]))
        assert [r.fragments[0].cells["Description"] for r in _data_rows(table)] == [
            "Long item first part", "Short item", "Long item second part tail"]
        assert _cells(table, "Long item second part tail").cells["Ref"] == "4803"

    def test_close_and_variable_width_columns(self):
        edges = [40.0, 58.0, 200.0, 222.0, 244.0, 300.0]
        _rep, [table] = _grids(F.grid(F.numeric_rows(4), header=[["N", "Description", "Q", "U", "Ref"]], edges=edges))
        assert _cells(table, "Item 2").cells == {"N": "2", "Description": "Item 2", "Q": "3,0", "U": "m", "Ref": "4702"}

    def test_hierarchical_header_keeps_parent_and_leaves(self):
        _rep, [table] = _grids(F.grid(F.numeric_rows(3), header=[["ID", "Description", "Measure", "", "Ref"],
                                                                 ["", "", "Qty", "Unit", ""]], header_spans=[(0, 2, 3)]))
        assert table.schema == ["ID", "Description", "Measure > Qty", "Measure > Unit", "Ref"]

    def test_row_groups_become_context(self):
        _rep, [table] = _grids(F.grid(F.numeric_rows(4), header=[B.SCHEMA], groups=("Group 1", "Group 2"), group_at=(2,)))
        rows = _data_rows(table)
        assert [r.context_label for r in rows] == ["Group 1", "Group 1", "Group 2", "Group 2"]

    def test_similar_objects_stay_distinct_rows(self):
        _rep, [table] = _grids(F.grid(F.similar_objects_rows(), header=[B.SCHEMA]))
        assert _cells(table, "Pump A").cells["Qty"] == "866,2" and _cells(table, "Pump B").cells["Qty"] == "912,4"

    def test_skewed_glyphs_are_read_by_the_native_engine(self):
        _rep, [table] = _grids(F.grid(F.numeric_rows(3), header=[B.SCHEMA], skew=True))
        assert _cells(table, "Item 2").cells["Ref"] == "4702"


class TestRefusal:
    def test_top_aligned_multiline_cells_are_not_certified(self):
        """Cellules non centrées : la rangée d'une ligne de texte n'est pas démontrable."""
        rep, tables = _grids(F.grid(F.multiline_rows(), header=[B.SCHEMA], align="top"))
        certified = {r.fragments[0].cells.get("Ref") for t in tables for r in _data_rows(t)}
        assert "4801" not in certified and "4803" not in certified
        assert any(o.get("rows_refused") for o in rep.outcomes if "ruleless" in (o.get("reason") or ""))

    def test_word_crossing_a_column_rule_refuses_the_body(self):
        rep, tables = _grids(F.grid(F.numeric_rows(4), header=[B.SCHEMA], straddle_at=1))
        assert not tables
        assert any(o.get("reason") == "ruleless_grid_rows_not_proven" for o in rep.outcomes)

    @pytest.mark.parametrize("header", [
        pytest.param(None, id="no-header-band"),
        pytest.param([["ID", "Description", "2026", "Unit", "Ref"]], id="numeric-header"),
        pytest.param([["ID", "Description", "Value", "Value", "Ref"]], id="duplicate-header"),
    ])
    def test_unproven_header_is_refused(self, header):
        _rep, tables = _grids(F.grid(F.numeric_rows(4), header=header))
        assert not tables

    def test_aligned_prose_without_rules_is_not_a_table(self):
        rep, tables = _grids(F.aligned_prose())
        assert not tables and not rep.tables

    def test_adjacent_grids_are_never_merged(self):
        _rep, tables = _grids(F.adjacent_grids())
        assert all(len(_data_rows(t)) <= 3 for t in tables)
        refs = [r.fragments[0].cells.get("Ref") for t in tables for r in _data_rows(t)]
        assert len(refs) == len(set(refs))

    def test_single_row_is_not_enough(self):
        rep, tables = _grids(F.grid(F.numeric_rows(1), header=[B.SCHEMA]))
        assert not tables

    def test_page_furniture_is_left_to_the_layout_analyser(self):
        _rep, tables = _grids(F.cartouche_only())
        assert not tables
