"""états de cellule du rail tabulaire générique (`cell_states`).

 a prouvé que `SourceFragment` confond une cellule VIDE (la
source ne porte aucune valeur) et une cellule ILLISIBLE (la source porte une valeur
qu'on ne sait pas lire), et perd les statuts approximatif et ambigu. Une cellule
illisible indexée comme vide est une fausse affirmation : « rien ici ».

L'extension est ADDITIVE : un fragment natif n'a aucun état, et son texte comme ses
métadonnées doivent rester strictement inchangés.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.extraction import pdf_tables as P

from . import _pdf_table_fixtures as F

SCHEMA = ["2024 > H1", "2024 > H2", "Total"]


def _table():
    return P.LogicalTable(table_id="t0", table_type=P.TABLE_TYPE_MATRIX, schema=list(SCHEMA),
                          column_paths=[["2024", "H1"], ["2024", "H2"], ["Total"]], source_pages=[3])


def _row(fragment):
    row = P.LogicalRow(logical_row_id="t0:r0", row_kind=P.ROW_KIND_DATA, row_label="South")
    row.fragments.append(fragment)
    return row


def _fragment(cells, states=None):
    kwargs = {"page_number": 3, "physical_row_index": 2, "cells": cells}
    if states is not None:
        kwargs["cell_states"] = states
    return P.SourceFragment(**kwargs)


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
class TestStructure:
    def test_field_exists_and_defaults_to_empty(self):
        fr = P.SourceFragment(page_number=1, physical_row_index=0, cells={"A": "1"})
        assert fr.cell_states == {}

    def test_default_is_not_shared_between_fragments(self):
        a = P.SourceFragment(page_number=1, physical_row_index=0, cells={})
        b = P.SourceFragment(page_number=1, physical_row_index=1, cells={})
        assert a.cell_states is not b.cell_states


# --------------------------------------------------------------------------- #
# Sérialisation texte
# --------------------------------------------------------------------------- #
class TestText:
    def test_unreadable_is_explicit_not_blank(self):
        fr = _fragment({"Total": "210"}, {"2024 > H1": "unreadable"})
        text = P.fragment_to_text(_table(), _row(fr), fr)
        assert "2024 > H1:\n[unreadable]" in text

    def test_approximate_is_marked(self):
        fr = _fragment({"2024 > H2": "95"}, {"2024 > H2": "approximate"})
        text = P.fragment_to_text(_table(), _row(fr), fr)
        assert "2024 > H2:\n95 (approximate)" in text

    def test_ambiguous_is_marked(self):
        fr = _fragment({"2024 > H2": "80"}, {"2024 > H2": "ambiguous"})
        text = P.fragment_to_text(_table(), _row(fr), fr)
        assert "2024 > H2:\n80 (ambiguous)" in text

    def test_blank_stays_omitted(self):
        fr = _fragment({"Total": "210"}, {"2024 > H1": "unreadable"})
        text = P.fragment_to_text(_table(), _row(fr), fr)
        assert "2024 > H2" not in text

    def test_exact_value_unchanged(self):
        fr = _fragment({"Total": "210"}, {"2024 > H1": "unreadable"})
        text = P.fragment_to_text(_table(), _row(fr), fr)
        assert "Total:\n210" in text
        assert "210 (" not in text

    def test_column_order_follows_schema(self):
        fr = _fragment({"Total": "210", "2024 > H2": "95"},
                       {"2024 > H1": "unreadable", "2024 > H2": "approximate"})
        text = P.fragment_to_text(_table(), _row(fr), fr)
        assert text.index("2024 > H1") < text.index("2024 > H2") < text.index("Total")


# --------------------------------------------------------------------------- #
# Métadonnées
# --------------------------------------------------------------------------- #
class TestMeta:
    def test_states_exposed_when_present(self):
        fr = _fragment({"Total": "210"}, {"2024 > H1": "unreadable"})
        meta = P.fragment_to_meta(_table(), _row(fr), fr)
        assert meta["cell_states"] == {"2024 > H1": "unreadable"}

    def test_unreadable_requires_visual_check(self):
        fr = _fragment({"Total": "210"}, {"2024 > H1": "unreadable"})
        assert P.fragment_to_meta(_table(), _row(fr), fr)["requires_visual_check"] is True

    def test_ambiguous_requires_visual_check(self):
        fr = _fragment({"2024 > H2": "80"}, {"2024 > H2": "ambiguous"})
        assert P.fragment_to_meta(_table(), _row(fr), fr)["requires_visual_check"] is True

    def test_approximate_alone_does_not_require_visual_check(self):
        """Choix documenté : une valeur approximative est ÉTIQUETÉE comme telle dans le
        texte indexé ; elle n'appelle pas à elle seule une vérification humaine."""
        fr = _fragment({"2024 > H2": "95"}, {"2024 > H2": "approximate"})
        meta = P.fragment_to_meta(_table(), _row(fr), fr)
        assert meta["cell_states"] == {"2024 > H2": "approximate"}
        assert meta["requires_visual_check"] is False

    def test_columns_present_excludes_unreadable(self):
        """`columns_present` garde son sens : colonnes portant une valeur lue."""
        fr = _fragment({"Total": "210"}, {"2024 > H1": "unreadable"})
        assert P.fragment_to_meta(_table(), _row(fr), fr)["columns_present"] == ["Total"]


# --------------------------------------------------------------------------- #
# Rétrocompatibilité du rail natif (§54, §129) — sans xfail : doit TENIR avant ET après
# --------------------------------------------------------------------------- #
def _legacy_text(table, row, fragment) -> str:
    """Sérialisation de référence AVANT l'extension, recopiée à l'identique."""
    lines = []
    if row.context_label:
        lines.append(f"Context: {row.context_label}")
    if row.row_label:
        lines.append(f"Row: {row.row_label}")
    for label in table.schema:
        value = fragment.cells.get(label)
        if value:
            lines.append(f"{label}:\n{value}")
    return "\n\n".join(lines).strip()


LEGACY_META_KEYS = {"kind", "table_type", "table_id", "logical_row_id", "row_kind", "row_label",
                    "context_label", "column_schema", "columns_present", "source_page",
                    "logical_row_pages", "continuation", "table_pages", "requires_visual_check"}
#: Provenance ADDITIVE (NATIVE-TABLE-RECOVERY-2) : coordonnées des cellules, toujours présentes
#: pour une ligne de données ; source de l'en-tête, seulement quand il vient d'un bandeau réglé.
PROVENANCE_META_KEYS = {"cell_boxes", "header_source"}

NATIVE_FIXTURES = [
    F.flat_three_column, F.flat_five_column, F.headerless_grid, F.repeated_header_two_pages,
    F.row_continuation_two_pages, F.continuation_then_new_row, F.blank_first_cell_new_row,
    F.legitimate_blank_cells, F.sparse_row, F.no_row_identifier, F.hierarchical_matrix,
    F.separator_row, F.row_across_three_pages,
]


@pytest.mark.parametrize("builder", NATIVE_FIXTURES, ids=lambda f: f.__name__)
class TestNativeParity:
    def test_text_identical_to_legacy_serializer(self, builder):
        for t in P.extract_generic_tables(builder()).tables:
            for r in t.rows:
                for fr in r.fragments:
                    assert P.fragment_to_text(t, r, fr) == _legacy_text(t, r, fr)

    def test_metadata_keys_unchanged(self, builder):
        for t in P.extract_generic_tables(builder()).tables:
            for r in t.rows:
                for fr in r.fragments:
                    meta = P.fragment_to_meta(t, r, fr)
                    assert LEGACY_META_KEYS <= set(meta) <= LEGACY_META_KEYS | PROVENANCE_META_KEYS
                    assert meta["requires_visual_check"] is False
                    if fr.cells:
                        assert set(meta["cell_boxes"]) == set(fr.cells)
