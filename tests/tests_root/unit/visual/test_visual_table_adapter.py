"""TablePayload Gateway -> rail tabulaire générique existant."""
from __future__ import annotations

import copy
import json

import pytest

from apps.rag_core.services.ingest.extraction import pdf_tables as P
from apps.rag_core.services.ingest.visual import table_adapter as A

from . import _vision_mocks as M

DIGEST = "a" * 64


def _adapt(payload=None, **kw):
    params = {"source_digest": DIGEST, "source_page": 4}
    params.update(kw)
    return A.adapt_table_payload(copy.deepcopy(payload or M.TABLE), **params)


def _cell(row, col, role, state="filled", text="x", certainty="exact", rs=1, cs=1):
    if state != "filled":
        text = None
        certainty = "unreadable" if state == "unreadable" else "exact"
    return {"row": row, "col": col, "row_span": rs, "col_span": cs, "role": role, "state": state,
            "text": text, "certainty": certainty}


def _payload(n_rows, n_cols, cells):
    return {"n_rows": n_rows, "n_cols": n_cols, "caption": None, "notes": [], "cells": cells}


def _rows(vt):
    return {r.row_label: r for r in vt.table.rows}


# --------------------------------------------------------------------------- #
# Golden
# --------------------------------------------------------------------------- #
class TestGolden:
    def test_partition(self):
        vt = _adapt()
        assert (vt.header_rows, vt.row_header_columns, vt.flat_fallback) == (2, 1, False)

    def test_existing_rail_structures(self):
        vt = _adapt()
        assert isinstance(vt.table, P.LogicalTable)
        assert all(isinstance(r, P.LogicalRow) for r in vt.table.rows)
        assert all(isinstance(f, P.SourceFragment) for r in vt.table.rows for f in r.fragments)
        assert vt.table.table_type == P.TABLE_TYPE_MATRIX

    def test_header_paths_merged_header(self):
        vt = _adapt()
        assert vt.table.schema == ["2024 > H1", "2024 > H2", "Total"]
        assert vt.table.column_paths == [["2024", "H1"], ["2024", "H2"], ["Total"]]

    def test_row_labels_and_corner_context(self):
        rows = _rows(_adapt())
        assert list(rows) == ["North", "South", "West"]
        assert {r.context_label for r in rows.values()} == {"Region"}

    def test_exact_and_blank(self):
        fr = _rows(_adapt())["North"].fragments[0]
        assert fr.cells == {"2024 > H1": "120", "Total": "120"}
        assert fr.cell_states == {}

    def test_unreadable_approximate(self):
        fr = _rows(_adapt())["South"].fragments[0]
        assert fr.cells == {"2024 > H2": "95", "Total": "210"}
        assert fr.cell_states == {"2024 > H1": "unreadable", "2024 > H2": "approximate"}

    def test_merged_data_emitted_once_and_ambiguous(self):
        vt = _adapt()
        row = _rows(vt)["West"]
        fr = row.fragments[0]
        assert fr.cells == {"2024 > H1": "80", "Total": "80"}
        assert fr.cell_states == {"2024 > H1": "ambiguous"}
        assert vt.merged_cells == [{"logical_row_id": row.logical_row_id, "column": "2024 > H1",
                                    "row_span": 1, "col_span": 2}]

    def test_serialized_text(self):
        vt = _adapt()
        row = _rows(vt)["South"]
        assert P.fragment_to_text(vt.table, row, row.fragments[0]) == (
            "Context: Region\n\nRow: South\n\n2024 > H1:\n[unreadable]\n\n"
            "2024 > H2:\n95 (approximate)\n\nTotal:\n210")
        north = _rows(vt)["North"]
        text = P.fragment_to_text(vt.table, north, north.fragments[0])
        assert "2024 > H2" not in text  # vide : omis, pas marqué illisible

    def test_meta(self):
        vt = _adapt()
        rows = _rows(vt)
        metas = {k: P.fragment_to_meta(vt.table, r, r.fragments[0]) for k, r in rows.items()}
        assert "cell_states" not in metas["North"]
        assert metas["North"]["requires_visual_check"] is False
        assert metas["South"]["requires_visual_check"] is True
        assert metas["West"]["requires_visual_check"] is True
        assert metas["South"]["source_page"] == 4
        assert metas["South"]["table_type"] == P.TABLE_TYPE_MATRIX
        assert M.IMAGE_B64 not in json.dumps(metas)

    def test_deterministic_ids(self):
        a, b = _adapt(), _adapt()
        assert a.table.table_id == b.table.table_id
        assert [r.logical_row_id for r in a.table.rows] == [f"{a.table.table_id}:r{i}" for i in range(3)]
        assert _adapt(occurrence=1).table.table_id != a.table.table_id

    def test_gateway_flag_carried(self):
        assert _adapt(requires_visual_check=True).requires_visual_check is True
        assert _adapt().requires_visual_check is False


# --------------------------------------------------------------------------- #
# Cas génériques
# --------------------------------------------------------------------------- #
class TestGeneric:
    def test_flat_table_without_row_headers(self):
        vt = _adapt(_payload(2, 2, [_cell(0, 0, "column_header", text="A"),
                                    _cell(0, 1, "column_header", text="B"),
                                    _cell(1, 0, "data", text="1"), _cell(1, 1, "data", text="2")]))
        assert (vt.header_rows, vt.row_header_columns) == (1, 0)
        assert vt.table.table_type == P.TABLE_TYPE_GENERIC
        assert vt.table.rows[0].row_label is None
        assert vt.table.rows[0].fragments[0].cells == {"A": "1", "B": "2"}

    def test_headerless_uses_neutral_columns(self):
        vt = _adapt(_payload(1, 2, [_cell(0, 0, "data", text="1"), _cell(0, 1, "data", text="2")]))
        assert vt.table.schema == ["Column 1", "Column 2"]

    def test_unreadable_header_placeholder_and_state(self):
        vt = _adapt(_payload(2, 2, [_cell(0, 0, "column_header", text="A"),
                                    _cell(0, 1, "column_header", state="unreadable"),
                                    _cell(1, 0, "data", text="1"), _cell(1, 1, "data", text="2")]))
        assert vt.table.schema == ["A", "[unreadable header]"]
        assert vt.header_states == {"[unreadable header]": "unreadable"}

    def test_non_unique_paths_fall_back_to_flat(self):
        vt = _adapt(_payload(2, 2, [_cell(0, 0, "column_header", text="Same"),
                                    _cell(0, 1, "column_header", text="Same"),
                                    _cell(1, 0, "data", text="1"), _cell(1, 1, "data", text="2")]))
        assert vt.flat_fallback is True
        assert vt.table.schema == ["Column 1", "Column 2"]
        assert vt.table.table_type == P.TABLE_TYPE_GENERIC
        assert vt.table.rows[0].fragments[0].cells == {"Column 1": "1", "Column 2": "2"}

    def test_multiple_row_header_columns_and_vertical_merge(self):
        cells = [
            _cell(0, 0, "corner", text="Group"), _cell(0, 1, "corner", text="Item"),
            _cell(0, 2, "column_header", text="Value"),
            _cell(1, 0, "row_header", text="G1", rs=2), _cell(1, 1, "row_header", text="a"),
            _cell(1, 2, "data", text="1"),
            _cell(2, 1, "row_header", text="b"), _cell(2, 2, "data", text="2"),
        ]
        vt = _adapt(_payload(3, 3, cells))
        assert vt.row_header_columns == 2
        assert [r.row_label for r in vt.table.rows] == ["G1 > a", "G1 > b"]
        assert vt.table.rows[0].context_label == "Group > Item"

    def test_row_header_not_assumed_first_column_only(self):
        """Colonne 0 = données : v = 0, aucune étiquette de ligne inventée."""
        vt = _adapt(_payload(2, 2, [_cell(0, 0, "column_header", text="A"),
                                    _cell(0, 1, "column_header", text="B"),
                                    _cell(1, 0, "data", text="x"), _cell(1, 1, "data", text="y")]))
        assert vt.row_header_columns == 0

    def test_vertical_merged_data_emitted_once(self):
        cells = [_cell(0, 0, "column_header", text="A"), _cell(0, 1, "column_header", text="B"),
                 _cell(1, 0, "data", text="span", rs=2), _cell(1, 1, "data", text="1"),
                 _cell(2, 1, "data", text="2")]
        vt = _adapt(_payload(3, 2, cells))
        assert [r.fragments[0].cells for r in vt.table.rows] == [{"A": "span", "B": "1"}, {"B": "2"}]

    def test_invalid_grid_rejected(self):
        with pytest.raises(ValueError):
            _adapt(_payload(1, 2, [_cell(0, 0, "data", text="1")]))

    def test_note_cells_go_to_notes(self):
        cells = [_cell(0, 0, "column_header", text="A"), _cell(1, 0, "data", text="1"),
                 _cell(2, 0, "note", text="footnote")]
        vt = _adapt(_payload(3, 1, cells))
        assert vt.notes == ["footnote"]
        assert all("footnote" not in r.fragments[0].cells.values() for r in vt.table.rows)
