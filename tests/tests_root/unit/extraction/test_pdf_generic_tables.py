"""contrat PROPRIÉTAIRE du moteur de tableaux GÉNÉRIQUE.

Ces tests décrivent le contrat du moteur générique. Ils n'emploient QUE du
vocabulaire neutre : si l'un d'eux ne passait qu'avec les en-têtes du corpus de
qualification, le moteur serait spécialisé et le lot serait NO-GO. Le corpus réel
n'intervient que plus tard, comme oracle.

Deux invariants structurent l'ensemble :

- l'identité de ligne est STRUCTURELLE, jamais textuelle (aucun identifiant du type
  `R-1` n'est requis pour qu'une ligne existe) ;
- une première cellule vide ne suffit JAMAIS à déclarer une continuation ; il faut
  un faisceau de signaux géométriques concordants.

L'import du module est fait DANS le corps des tests : c'est ce qui a permis au
cycle xfail strict du lot de produire des ÉCHECS de test, et non une erreur de
collecte, tant que le module n'existait pas.
"""
from __future__ import annotations

import pytest

from . import _pdf_table_fixtures as F


# --------------------------------------------------------------------------- #
# Accès à l'API visée (import tardif : voir docstring)
# --------------------------------------------------------------------------- #
def _api():
    from apps.rag_core.services.ingest.extraction import pdf_tables

    return pdf_tables


def _report(data: bytes):
    return _api().extract_generic_tables(data)


def _tables(data: bytes):
    return _report(data).tables


def _one(data: bytes):
    ts = _tables(data)
    assert len(ts) == 1, f"un seul tableau logique attendu, obtenu {len(ts)}"
    return ts[0]


def _data_rows(table):
    return [r for r in table.rows if r.row_kind == _api().ROW_KIND_DATA]


def _cells_on(row, page_number):
    for fr in row.fragments:
        if fr.page_number == page_number:
            return dict(fr.cells)
    return {}


def _merged(row) -> dict:
    """Vue applatie de la ligne logique, tous fragments confondus."""
    out = {}
    for fr in row.fragments:
        for k, v in fr.cells.items():
            out.setdefault(k, []).append(v)
    return {k: " ".join(v) for k, v in out.items()}


def _text(row) -> str:
    return " ".join(v for fr in row.fragments for v in fr.cells.values())


# --------------------------------------------------------------------------- #
# §62 / §4.A — tableau plat 3 colonnes
# --------------------------------------------------------------------------- #
class TestFlatTable:
    def test_detected_with_its_schema(self):
        t = _one(F.flat_three_column())
        assert t.table_type == _api().TABLE_TYPE_GENERIC
        assert t.schema == F.SCHEMA_3

    def test_rows_and_columns(self):
        rows = _data_rows(_one(F.flat_three_column()))
        assert len(rows) == 2
        assert _cells_on(rows[0], 1) == {
            "ID": "R-1", "Description": "First item description", "Amount": "10.00"}

    def test_source_page(self):
        rows = _data_rows(_one(F.flat_three_column()))
        assert [fr.page_number for fr in rows[0].fragments] == [1]

    def test_no_fixed_column_count(self):
        """§10 : 5 colonnes doivent marcher exactement comme 3."""
        t = _one(F.flat_five_column())
        assert t.schema == F.SCHEMA_5
        assert len(_data_rows(t)) == 3


# --------------------------------------------------------------------------- #
# §63 — tableau SANS en-tête textuel
# --------------------------------------------------------------------------- #
class TestHeaderlessTable:
    def test_neutral_column_labels(self):
        t = _one(F.headerless_grid())
        assert t.schema == ["Column 1", "Column 2", "Column 3"]

    def test_no_row_is_consumed_as_a_header(self):
        t = _one(F.headerless_grid())
        assert len(_data_rows(t)) == 3

    def test_no_invented_semantics(self):
        t = _one(F.headerless_grid())
        assert _cells_on(_data_rows(t)[0], 1) == {
            "Column 1": "1", "Column 2": "2", "Column 3": "3"}


# --------------------------------------------------------------------------- #
# §64 / §4.G — colonnes d'espacement physiques
# --------------------------------------------------------------------------- #
class TestSpacerColumns:
    @pytest.mark.parametrize("physical", [3, 6, 9])
    def test_same_normalized_schema(self, physical):
        assert _one(F.spacer_columns(physical)).schema == F.SCHEMA_3

    @pytest.mark.parametrize("physical", [3, 6, 9])
    def test_no_unnamed_column_survives(self, physical):
        t = _one(F.spacer_columns(physical))
        assert all((c or "").strip() for c in t.schema), t.schema

    @pytest.mark.parametrize("physical", [3, 6, 9])
    def test_same_semantic_rows(self, physical):
        rows = _data_rows(_one(F.spacer_columns(physical)))
        assert len(rows) == 2
        assert _cells_on(rows[1], 1)["Amount"] == "20.50"


# --------------------------------------------------------------------------- #
# §66 / §4.C — en-tête répétée sur plusieurs pages
# --------------------------------------------------------------------------- #
class TestRepeatedHeader:
    def test_single_logical_table(self):
        t = _one(F.repeated_header_two_pages())
        assert t.page_start == 1 and t.page_end == 2
        assert t.source_pages == [1, 2]

    def test_header_is_never_a_data_row(self):
        t = _one(F.repeated_header_two_pages())
        for row in _data_rows(t):
            assert "Description" not in _text(row) or "Description " in _text(row)
            assert _text(row).strip() != "ID Description Amount"

    def test_all_rows_present_once(self):
        rows = _data_rows(_one(F.repeated_header_two_pages()))
        assert [_cells_on(r, p).get("ID") for r, p in
                zip(rows, [1, 1, 1, 2, 2])] == ["R-1", "R-2", "R-3", "R-4", "R-5"]


# --------------------------------------------------------------------------- #
# §67 / §4.D — ligne logique à cheval sur deux pages, SANS identifiant
# --------------------------------------------------------------------------- #
class TestRowContinuation:
    def test_one_logical_row_over_two_pages(self):
        rows = _data_rows(_one(F.row_continuation_two_pages()))
        assert len(rows) == 1, [r.row_label for r in rows]
        assert sorted(fr.page_number for fr in rows[0].fragments) == [1, 2]

    def test_continuation_keeps_its_own_column(self):
        row = _data_rows(_one(F.row_continuation_two_pages()))[0]
        assert _cells_on(row, 2) == {"Description": "Second part of the same description"}

    def test_nothing_is_copied_into_the_blank_cells(self):
        row = _data_rows(_one(F.row_continuation_two_pages()))[0]
        p2 = _cells_on(row, 2)
        assert "ID" not in p2 and "Amount" not in p2

    def test_row_identity_is_structural_not_textual(self):
        row = _data_rows(_one(F.row_continuation_two_pages()))[0]
        assert row.logical_row_id
        assert row.source_pages == [1, 2]


# --------------------------------------------------------------------------- #
# §68 / §4.E — continuation PUIS nouvelle ligne sur la même page
# --------------------------------------------------------------------------- #
class TestContinuationThenNewRow:
    def test_exactly_two_logical_rows(self):
        rows = _data_rows(_one(F.continuation_then_new_row()))
        assert len(rows) == 2

    def test_continuation_belongs_to_the_first_row(self):
        rows = _data_rows(_one(F.continuation_then_new_row()))
        assert _cells_on(rows[0], 2)["Description"] == "Second part of the same description"

    def test_new_row_does_not_absorb_the_continuation(self):
        rows = _data_rows(_one(F.continuation_then_new_row()))
        assert "Second part" not in _text(rows[1]), _text(rows[1])
        assert _merged(rows[1])["ID"] == "R-2"

    def test_first_row_does_not_absorb_the_new_row(self):
        rows = _data_rows(_one(F.continuation_then_new_row()))
        assert "entirely different" not in _text(rows[0]), _text(rows[0])


# --------------------------------------------------------------------------- #
# §69 — PREUVE que « première cellule vide » ne suffit PAS
# --------------------------------------------------------------------------- #
class TestBlankFirstCellIsNotContinuation:
    def test_blank_anchor_mid_table_stays_a_new_row(self):
        rows = _data_rows(_one(F.blank_first_cell_new_row()))
        assert len(rows) == 3, [_merged(r) for r in rows]

    def test_its_content_is_not_merged_into_the_previous_row(self):
        rows = _data_rows(_one(F.blank_first_cell_new_row()))
        assert "legitimate row" not in _text(rows[0])
        assert "legitimate row" in _text(rows[1])

    def test_the_blank_anchor_row_has_no_label(self):
        rows = _data_rows(_one(F.blank_first_cell_new_row()))
        assert rows[1].row_label in (None, "")


# --------------------------------------------------------------------------- #
# §4.F / §4.O / §32 — cellules vides légitimes
# --------------------------------------------------------------------------- #
class TestBlankCells:
    def test_blank_stays_blank(self):
        rows = _data_rows(_one(F.legitimate_blank_cells()))
        assert "Description" not in _cells_on(rows[0], 1)
        assert "Amount" not in _cells_on(rows[1], 1)

    def test_no_neighbour_is_copied(self):
        rows = _data_rows(_one(F.legitimate_blank_cells()))
        assert _cells_on(rows[0], 1)["Amount"] == "10.00"
        assert _cells_on(rows[1], 1)["Description"] == "Second item description"


# --------------------------------------------------------------------------- #
# §74 / §47 — ligne éparse
# --------------------------------------------------------------------------- #
class TestSparseRow:
    def test_sparse_row_is_its_own_row(self):
        rows = _data_rows(_one(F.sparse_row()))
        assert len(rows) == 2

    def test_only_the_filled_columns_are_present(self):
        rows = _data_rows(_one(F.sparse_row()))
        assert _cells_on(rows[1], 1) == {"Label": "Beta", "Unit price": "4.00"}


# --------------------------------------------------------------------------- #
# §4.N / §25 — tableau sans identifiant de ligne
# --------------------------------------------------------------------------- #
class TestNoRowIdentifier:
    def test_rows_exist_without_any_textual_key(self):
        t = _one(F.no_row_identifier())
        rows = _data_rows(t)
        assert t.schema == ["Description", "Amount"]
        assert len(rows) == 2
        assert len({r.logical_row_id for r in rows}) == 2


# --------------------------------------------------------------------------- #
# §70 / §71 / §72 — l'égalité de schéma NE fusionne PAS
# --------------------------------------------------------------------------- #
class TestSameSchemaDoesNotMerge:
    def test_consecutive_pages_stay_independent(self):
        ts = _tables(F.independent_tables_consecutive_pages())
        assert len(ts) == 2, [t.source_pages for t in ts]
        assert [t.source_pages for t in ts] == [[1], [2]]

    def test_same_page_stays_independent(self):
        ts = _tables(F.independent_tables_same_page())
        assert len(ts) == 2, [t.source_pages for t in ts]

    def test_prose_boundary_stays_independent(self):
        ts = _tables(F.tables_separated_by_prose())
        assert len(ts) == 2, [t.source_pages for t in ts]

    def test_table_ids_differ(self):
        ts = _tables(F.independent_tables_consecutive_pages())
        assert ts[0].table_id != ts[1].table_id


# --------------------------------------------------------------------------- #
# §65 / §75 / §76 — matrice à en-tête hiérarchique
# --------------------------------------------------------------------------- #
class TestHierarchicalMatrix:
    def test_typed_as_matrix(self):
        assert _one(F.hierarchical_matrix()).table_type == _api().TABLE_TYPE_MATRIX

    def test_column_paths_are_hierarchical(self):
        t = _one(F.hierarchical_matrix())
        assert t.column_paths == [["Q1", "A"], ["Q1", "B"], ["Q2", "A"], ["Q2", "B"]]

    def test_row_paths_and_values(self):
        t = _one(F.hierarchical_matrix())
        rows = _data_rows(t)
        assert [r.row_label for r in rows] == ["Row1", "Row2"]
        assert _cells_on(rows[0], 1) == {
            "Q1 > A": "1", "Q1 > B": "2", "Q2 > A": "3", "Q2 > B": "4"}

    def test_negative_no_crossed_association(self):
        """§76 : Row1/Q1>A ne doit jamais valoir la valeur de Q2>B."""
        rows = _data_rows(_one(F.hierarchical_matrix()))
        assert _cells_on(rows[0], 1)["Q1 > A"] != "4"
        assert _cells_on(rows[1], 1)["Q2 > B"] == "8"


# --------------------------------------------------------------------------- #
# §44 / §45 — rangée séparatrice, sans interprétation sémantique
# --------------------------------------------------------------------------- #
class TestSeparatorRow:
    def test_full_width_row_is_typed_separator(self):
        t = _one(F.separator_row())
        seps = [r for r in t.rows if r.row_kind == _api().ROW_KIND_SEPARATOR]
        assert len(seps) == 1
        assert seps[0].row_label == F.SEPARATOR_LABEL

    def test_separator_is_not_a_data_row(self):
        t = _one(F.separator_row())
        assert len(_data_rows(t)) == 1

    def test_following_row_carries_the_context_label(self):
        t = _one(F.separator_row())
        assert _data_rows(t)[0].context_label == F.SEPARATOR_LABEL


# --------------------------------------------------------------------------- #
# §73 / §4.M — faux positif à une colonne
# --------------------------------------------------------------------------- #
class TestOneColumnFalsePositive:
    def test_rejected(self):
        assert _tables(F.one_column_box()) == []

    def test_outcome_is_recorded_as_rejected(self):
        rep = _report(F.one_column_box())
        assert rep.outcomes, "le rejet doit être tracé"
        assert all(o["outcome"] == _api().OUTCOME_REJECTED for o in rep.outcomes), rep.outcomes


# --------------------------------------------------------------------------- #
# §77 / §52 — repli sûr sur candidat non structurable
# --------------------------------------------------------------------------- #
class TestAmbiguousFallback:
    def test_no_table_is_invented(self):
        assert _tables(F.ambiguous_ragged()) == []

    def test_region_is_not_suppressed(self):
        """Un candidat non structuré ne doit JAMAIS retirer son texte de la page."""
        rep = _report(F.ambiguous_ragged())
        assert rep.suppressed_regions.get(1, []) == []

    def test_bounded_warning(self):
        rep = _report(F.ambiguous_ragged())
        assert len(rep.warnings) <= 2, rep.warnings


# --------------------------------------------------------------------------- #
# §23 / §97 — déterminisme des identités
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_three_runs_identical(self):
        api = _api()
        data = F.continuation_then_new_row()
        snaps = []
        for _ in range(3):
            snaps.append([
                (t.table_id, t.table_type, tuple(t.schema), t.page_start, t.page_end,
                 tuple((r.logical_row_id, r.row_label, r.row_kind,
                        tuple((fr.page_number, tuple(sorted(fr.cells.items())))
                              for fr in r.fragments))
                       for r in t.rows))
                for t in api.extract_generic_tables(data).tables
            ])
        assert snaps[0] == snaps[1] == snaps[2]

    def test_ids_are_not_random(self):
        api = _api()
        data = F.flat_three_column()
        a = api.extract_generic_tables(data).tables[0]
        b = api.extract_generic_tables(bytes(data)).tables[0]
        assert a.table_id == b.table_id
        assert [r.logical_row_id for r in a.rows] == [r.logical_row_id for r in b.rows]


# --------------------------------------------------------------------------- #
# §39 / §40 / §41 — sérialisation étiquetée
# --------------------------------------------------------------------------- #
class TestSerialization:
    def test_every_value_is_labelled_by_its_column(self):
        api = _api()
        t = _one(F.flat_three_column())
        row = _data_rows(t)[0]
        text = api.fragment_to_text(t, row, row.fragments[0])
        for label in F.SCHEMA_3:
            assert f"{label}:" in text, text
        assert "First item description" in text

    def test_headerless_uses_neutral_labels(self):
        api = _api()
        t = _one(F.headerless_grid())
        row = _data_rows(t)[0]
        text = api.fragment_to_text(t, row, row.fragments[0])
        assert "Column 1:" in text

    def test_matrix_serializes_the_full_path(self):
        api = _api()
        t = _one(F.hierarchical_matrix())
        row = _data_rows(t)[0]
        text = api.fragment_to_text(t, row, row.fragments[0])
        assert "Q1 > A:" in text, text

    def test_metadata_is_generic_and_page_accurate(self):
        api = _api()
        t = _one(F.row_continuation_two_pages())
        row = _data_rows(t)[0]
        metas = [api.fragment_to_meta(t, row, fr) for fr in row.fragments]
        assert [m["source_page"] for m in metas] == [1, 2]
        for m in metas:
            assert m["kind"] == "table"
            assert m["table_type"] == api.TABLE_TYPE_GENERIC
            assert m["table_id"] == t.table_id
            assert m["logical_row_id"] == row.logical_row_id
            assert m["logical_row_pages"] == [1, 2]
            assert m["column_schema"] == F.SCHEMA_3


# --------------------------------------------------------------------------- #
# NATIVE-TABLE-GUARD-1 — libellés d'en-tête dupliqués (constat réel : une valeur écrasée)
# --------------------------------------------------------------------------- #
def _rows_by_label(data):
    from apps.rag_core.services.ingest.extraction.pdf_tables import extract_generic_tables

    report = extract_generic_tables(data)
    assert len(report.tables) == 1
    table = report.tables[0]
    return table, {r.row_label: r.fragments[0].cells for r in table.rows}


def test_duplicate_header_labels_annex_the_textual_leaf_row():
    table, rows = _rows_by_label(F.duplicate_header_labels(with_leaf_row=True))
    assert len(set(table.schema)) == len(table.schema)
    low = next(s for s in table.schema if s.endswith("Low"))
    high = next(s for s in table.schema if s.endswith("High"))
    assert rows["Item A"][low] == "900" and rows["Item A"][high] == "800"
    assert "Low" not in rows and "" not in rows  # la rangée de feuilles n'est pas une donnée


def test_duplicate_header_labels_without_leaf_row_never_overwrite_a_cell():
    table, rows = _rows_by_label(F.duplicate_header_labels(with_leaf_row=False))
    assert len(set(table.schema)) == len(table.schema)
    values = [v for cells in rows.values() for v in cells.values()]
    assert "900" in values and "800" in values  # aucune valeur perdue
