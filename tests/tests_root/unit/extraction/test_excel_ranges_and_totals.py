"""P1.2 — plages exactes et héritage des en-têtes par les blocs TOTAL/SUBTOTAL.

Deux défauts levés par un classeur financier réel :

1. la plage citée était calculée avec `row_start + 1`, c'est-à-dire en supposant UNE
   ligne d'en-tête. Avec deux lignes (bandeau + intitulés), la citation désignait les
   mauvaises cellules — décalée d'un cran aux deux bornes ;

2. la ligne `SUBTOTAL`, séparée du tableau par une ligne vide, formait un bloc distinct
   sans en-tête. Son total partait à l'index sous `col2`, coupé de « Shares Outstanding ».
   Le total du classeur devenait introuvable, ce qui poussait le modèle à présenter la
   première catégorie venue comme le total de l'entreprise.
"""
from apps.rag_core.services.ingest.extraction.excel_blocks import (
    Cell,
    split_sheet_into_blocks,
)
from apps.rag_core.services.ingest.extraction.excel_render import render_block


def _grid(rows):
    """Grille de `Cell` : un int/float devient une cellule numérique."""
    out = []
    for r in rows:
        out.append([
            Cell(text="" if v is None else str(v),
                 is_numeric=isinstance(v, (int, float)) and not isinstance(v, bool))
            for v in r
        ])
    return out


def _blocks(rows, **kw):
    blocks, _truncated, warnings = split_sheet_into_blocks(_grid(rows), max_blocks=20, **kw)
    return blocks, warnings


def _render(block):
    chunks, _ = render_block(
        block, sheet_name="S", sheet_index=1, sheet_hidden=False,
        rows_per_chunk=40, chunk_max_chars=4000, max_columns_markdown=12,
        has_formulas=False,
    )
    return chunks


# --------------------------------------------------------------------------- #
# 1. Plages exactes — plus aucune profondeur d'en-tête codée en dur
# --------------------------------------------------------------------------- #
class TestExactRanges:
    def test_one_header_row(self):
        b, _ = _blocks([["Projet", "Budget"], ["Atlas", 250], ["Orion", 120]])
        blk = b[0]
        assert blk.header_rows == [1]
        assert blk.data_row_start == 2
        assert blk.header_range == "A1:B1"

        m = _render(blk)[0]["meta"]
        assert m["header_range"] == "A1:B1"
        assert m["data_range"] == "A2:B3"
        assert m["cell_range"] == "A2:B3"

    def test_two_header_rows(self):
        """Le cas du classeur réel : un bandeau de groupe au-dessus des intitulés."""
        b, _ = _blocks([
            [None, "Premoney", None],
            ["Security", "Shares", "Diluted"],
            ["Common", 10, 10],
            ["Series A", 20, 20],
        ])
        blk = b[0]
        assert blk.header_rows == [1, 2]
        assert blk.data_row_start == 3          # PAS 2 : c'était le bug

        m = _render(blk)[0]["meta"]
        assert m["header_range"] == "A1:C2"
        assert m["data_range"] == "A3:C4"
        assert m["cell_range"] == "A3:C4"

    def test_three_header_rows(self):
        b, _ = _blocks([
            [None, "Exercice 2026", None],
            [None, "Premoney", "Postmoney"],
            ["Security", "Shares", "Shares"],
            ["Common", 10, 10],
            ["Series A", 20, 20],
        ])
        blk = b[0]
        assert blk.header_rows == [1, 2, 3]
        assert blk.data_row_start == 4
        m = _render(blk)[0]["meta"]
        assert m["header_range"] == "A1:C3"
        assert m["data_range"] == "A4:C5"

    def test_no_header_at_all_starts_at_the_first_row(self):
        b, _ = _blocks([[1, 2], [3, 4]])
        blk = b[0]
        assert blk.headers_inferred is True
        assert blk.header_rows == []
        assert blk.data_row_start == 1          # aucune ligne consommée
        m = _render(blk)[0]["meta"]
        assert m["cell_range"] == "A1:B2"
        assert m["header_range"] is None

    def test_a_title_row_does_not_shift_the_coordinates(self):
        """Une ligne-titre isolée est absorbée ; le tableau garde ses vraies lignes."""
        b, _ = _blocks([
            ["Budget 2026", None],
            ["Projet", "Budget"],
            ["Atlas", 250],
        ])
        blk = b[0]
        assert blk.title == "Budget 2026"
        assert blk.header_rows == [2]
        assert blk.data_row_start == 3
        assert _render(blk)[0]["meta"]["cell_range"] == "A3:B3"

    def test_a_secondary_table_lower_in_the_sheet_keeps_real_coordinates(self):
        b, _ = _blocks([
            ["Projet", "Budget"],
            ["Atlas", 250],
            [None, None],
            [None, None],
            ["Region", "CA"],
            ["Nord", 900],
        ])
        second = b[1]
        assert second.row_start == 5
        assert second.header_rows == [5]
        assert second.data_row_start == 6
        m = _render(second)[0]["meta"]
        assert m["header_range"] == "A5:B5"
        assert m["cell_range"] == "A6:B6"

    def test_a_block_not_starting_at_column_a(self):
        b, _ = _blocks([
            [None, "Projet", "Budget"],
            [None, "Atlas", 250],
        ])
        blk = b[0]
        assert blk.col_start == 2
        m = _render(blk)[0]["meta"]
        assert m["header_range"] == "B1:C1"
        assert m["cell_range"] == "B2:C2"

    def test_chunk_windows_keep_exact_ranges(self):
        rows = [[None, "Premoney", None], ["Security", "Shares", "Diluted"]]
        rows += [[f"S{i}", i, i] for i in range(1, 7)]
        blk = _blocks(rows)[0][0]
        chunks, _ = render_block(
            blk, sheet_name="S", sheet_index=1, sheet_hidden=False,
            rows_per_chunk=3, chunk_max_chars=4000, max_columns_markdown=12,
            has_formulas=False,
        )
        assert [c["meta"]["cell_range"] for c in chunks] == ["A3:C5", "A6:C8"]
        # L'en-tête est répété dans CHAQUE chunk : sa plage aussi.
        assert all(c["meta"]["header_range"] == "A1:C2" for c in chunks)


# --------------------------------------------------------------------------- #
# 2. Héritage des en-têtes par un bloc TOTAL / SUBTOTAL
# --------------------------------------------------------------------------- #
_TABLE = [
    [None, "Premoney", None],
    ["Security", "Shares Outstanding", "Fully Diluted"],
    ["Common", 6234211, 6234211],
    ["Series A", 6666916, 6666916],
]


class TestTotalHeaderInheritance:
    def test_subtotal_after_one_blank_row_inherits(self):
        blocks, warnings = _blocks(_TABLE + [[None, None, None],
                                             ["SUBTOTAL", 23379395, 23379395]])
        total = blocks[1]
        assert total.headers_inherited is True
        assert total.headers == blocks[0].headers
        assert total.headers_inferred is False
        assert total.header_strategy == "inherited_total_block"
        assert total.inherited_from_table_index == 1
        assert total.source_header_range == "A1:C2"
        assert "excel_total_headers_inherited" in warnings

    def test_the_inherited_block_renders_real_headers(self):
        blocks, _ = _blocks(_TABLE + [[None, None, None],
                                      ["SUBTOTAL", 23379395, 23379395]])
        text = _render(blocks[1])[0]["text"]
        assert "Shares Outstanding" in text
        assert "col2" not in text
        assert "| SUBTOTAL | 23379395 | 23379395 |" in text

    def test_the_fallback_rendering_also_uses_real_headers(self):
        """Au-delà du seuil Markdown, le repli `col: valeur` doit aussi être nommé."""
        blocks, _ = _blocks(_TABLE + [[None, None, None],
                                      ["SUBTOTAL", 23379395, 23379395]])
        chunks, _ = render_block(
            blocks[1], sheet_name="S", sheet_index=1, sheet_hidden=False,
            rows_per_chunk=40, chunk_max_chars=4000,
            max_columns_markdown=1,          # force le repli
            has_formulas=False,
        )
        text = chunks[0]["text"]
        assert "Shares Outstanding: 23379395" in text
        assert "col2:" not in text

    def test_values_and_coordinates_are_never_moved(self):
        blocks, _ = _blocks(_TABLE + [[None, None, None],
                                      ["SUBTOTAL", 23379395, 23379395]])
        total = blocks[1]
        assert total.row_start == 6 and total.row_end == 6
        assert total.rows == [["SUBTOTAL", "23379395", "23379395"]]
        m = _render(total)[0]["meta"]
        assert m["cell_range"] == "A6:C6"       # les données du bloc, pas celles du tableau
        assert m["header_range"] == "A1:C2"     # l'en-tête emprunté, cité comme tel
        assert m["data_range"] == "A6:C6"

    def test_plain_total_and_grand_total_and_french_variants(self):
        for label in ["TOTAL", "Total", "SOUS-TOTAL", "Sous-total", "GRAND TOTAL",
                      "TOTAL GENERAL", "TOTAL GÉNÉRAL", "TOTAL :"]:
            blocks, _ = _blocks(_TABLE + [[None, None, None], [label, 1, 2]])
            assert blocks[1].headers_inherited is True, label

    def test_a_narrower_total_block_inherits_the_matching_prefix(self):
        """Les colonnes sont alignées par coordonnée absolue — jamais réalignées."""
        blocks, _ = _blocks(_TABLE + [[None, None, None], ["TOTAL", 23379395, None]])
        total = blocks[1]
        assert total.headers_inherited is True
        assert total.headers == blocks[0].headers[:2]      # préfixe strict, pas un décalage
        assert total.headers == ["Security", "Premoney / Shares Outstanding"]
        assert total.col_start == 1 and total.col_end == 2


class TestInheritanceIsRefusedWhenInDoubt:
    """En cas de doute : `col1..colN`. Un en-tête faux ment sur la nature de la colonne."""

    def _refused(self, tail, table=None):
        blocks, _ = _blocks((table or _TABLE) + tail)
        last = blocks[-1]
        assert last.headers_inherited is False
        assert last.headers_inferred is True
        assert last.headers[0] == "col1"
        return last

    def test_an_ordinary_business_row_does_not_inherit(self):
        self._refused([[None, None, None], ["Atlas", 250, 260]])

    def test_a_block_two_rows_below_does_not_inherit(self):
        self._refused([[None, None, None], [None, None, None],
                       ["TOTAL", 23379395, 23379395]])

    def test_a_wider_block_does_not_inherit(self):
        blocks, _ = _blocks([
            [None, "Premoney", None],
            ["Security", "Shares", "Diluted"],
            ["Common", 1, 2],
            [None, None, None, None],
            ["TOTAL", 1, 2, 99],
        ])
        assert blocks[1].headers_inherited is False

    def test_a_block_starting_on_another_column_does_not_inherit(self):
        self._refused([[None, None, None], [None, "TOTAL", 23379395]])

    def test_total_in_a_narrative_sentence_does_not_inherit(self):
        blocks, _ = _blocks(_TABLE + [
            [None, None, None],
            ["Le total des actions est indiqué en annexe", None, None],
            ["Voir la note 4 du rapport annuel", None, None],
        ])
        note = blocks[1]
        assert note.headers_inherited is False
        assert note.kind == "narrative"          # de la prose, pas une ligne de total

    def test_a_new_table_with_its_own_header_does_not_inherit(self):
        blocks, _ = _blocks(_TABLE + [[None, None, None],
                                      ["Region", "CA", "Marge"],
                                      ["Nord", 900, 80]])
        second = blocks[1]
        assert second.headers_inherited is False
        assert second.headers == ["Region", "CA", "Marge"]

    def test_an_intervening_title_row_breaks_the_inheritance(self):
        """Un titre entre les deux blocs ouvre une nouvelle section."""
        blocks, _ = _blocks(_TABLE + [[None, None, None],
                                      ["Synthèse", None, None],
                                      [None, None, None],
                                      ["TOTAL", 1, 2]])
        total = blocks[1]
        assert total.title == "Synthèse"
        assert total.headers_inherited is False

    def test_a_grand_total_below_a_subtotal_still_cites_the_real_table(self):
        """`TOTAL` puis `GRAND TOTAL` : les deux héritent, tous deux DU TABLEAU."""
        blocks, _ = _blocks(_TABLE + [[None, None, None], ["TOTAL", 1, 2],
                                      [None, None, None], ["GRAND TOTAL", 3, 4]])
        assert blocks[1].headers_inherited is True
        assert blocks[2].headers_inherited is True
        assert blocks[2].headers == blocks[0].headers
        # La provenance citée reste le TABLEAU (bloc 1), jamais le bloc intermédiaire.
        assert blocks[2].inherited_from_table_index == 1
        assert blocks[2].source_header_range == "A1:C2"

    def test_a_long_block_of_total_looking_rows_does_not_inherit(self):
        self._refused([[None, None, None], ["TOTAL", 1, 2], ["TOTAL", 3, 4],
                       ["TOTAL", 5, 6], ["TOTAL", 7, 8]])

    def test_a_total_block_after_a_headerless_table_does_not_inherit(self):
        blocks, _ = _blocks([[1, 2], [3, 4], [None, None], ["TOTAL", 6]])
        assert blocks[0].headers_inferred is True
        assert blocks[1].headers_inherited is False
