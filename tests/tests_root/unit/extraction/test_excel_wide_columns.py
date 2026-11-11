"""Feuilles larges : découpage bidimensionnel (lignes × COLONNES).

Le défaut n'était pas esthétique, il était fatal au retrieval.

Une feuille de capitalisation à 44 colonnes était découpée en 50 chunks de lignes, et
chacun répétait les 44 intitulés — « Series A-1 », « Inputted Cash Investment »,
« Voting Thresholds / … ». Les 50 chunks devenaient **lexicalement quasi identiques** :
une recherche sur « Cash Investment » les matchait tous, avec un `ts_rank`
**rigoureusement égal** (0,263827 pour les 51), et l'unique chunk qui portait la réponse
sortait 52e — au tirage.

Aucun classement ne peut départager une égalité parfaite. Ni RRF, ni la diversité, ni un
reranker : le problème n'était pas l'ORDRE, c'était que les chunks ne se DISTINGUAIENT
plus. Il fallait cesser de les rendre identiques.
"""
import pytest

from apps.rag_core.services.ingest.extraction.excel_blocks import (
    Cell,
    split_sheet_into_blocks,
)
from apps.rag_core.services.ingest.extraction.excel_columns import (
    build_column_windows,
    column_group,
    detect_identity_columns,
)
from apps.rag_core.services.ingest.extraction.excel_render import render_block

WIDE = 16          # DOCUMENT_EXCEL_WIDE_TABLE_THRESHOLD
PER_CHUNK = 8      # DOCUMENT_EXCEL_MAX_COLUMNS_PER_CHUNK


def _grid(rows):
    return [[Cell(text="" if v is None else str(v),
                  is_numeric=isinstance(v, (int, float)) and not isinstance(v, bool))
             for v in r] for r in rows]


def _block(rows):
    blocks, _t, _w = split_sheet_into_blocks(_grid(rows), max_blocks=20)
    return blocks[0]


def _render(block, **kw):
    opts = dict(
        sheet_name="S", sheet_index=1, sheet_hidden=False, rows_per_chunk=40,
        chunk_max_chars=4000, max_columns_markdown=20, has_formulas=False,
        wide_threshold=WIDE, max_columns_per_chunk=PER_CHUNK, max_chunks=500,
    )
    opts.update(kw)
    return render_block(block, **opts)


#: Le gabarit RÉEL : identité + trois bandeaux de 5 colonnes, séparés par des colonnes
#: vides. 18 colonnes au total — au-delà du seuil « large » (16).
_LABELS = ("Shares", "Diluted", "Pct", "Voting", "Basis")


def _cap_table(n_rows=4):
    header1 = ([None]
               + ["Premoney"] * 5 + [None]
               + ["AntiDil"] * 5 + [None]
               + ["Postmoney"] * 5)
    header2 = (["Security"]
               + list(_LABELS) + [None]
               + ["Issue Price", "Conv Price", "Ratio", "Waive", "Adj"] + [None]
               + list(_LABELS))
    rows = [header1, header2]
    names = ["Common", "Series A-1", "Series A-2", "Warrants", "Options", "SAFE"]
    for i in range(n_rows):
        rows.append([names[i % len(names)]]
                    + [100 + i, 200 + i, "1%", 5, 7] + [None]
                    + [1.4, 1.5, 1, "NO", 0] + [None]
                    + [300 + i, 400 + i, "2%", 9, 11])
    return rows


class TestColumnGroups:
    def test_the_parent_is_read_from_the_p11_hierarchy(self):
        assert column_group("Premoney Capitalization / Shares Outstanding") == "Premoney Capitalization"
        assert column_group("Security") is None

    def test_an_empty_column_is_a_boundary_the_author_drew(self):
        headers = ["A / x", "A / y", "", "B / x", "B / y"]
        rows = [["1", "2", "", "3", "4"]]
        windows, _ = build_column_windows(headers, rows, max_columns=PER_CHUNK)
        assert [w.columns for w in windows] == [(0, 1), (3, 4)]
        # La colonne vide n'est RENDUE nulle part : elle ne porterait qu'un « colN: ».
        assert all(2 not in w.columns for w in windows)

    def test_columns_of_the_same_group_stay_together(self):
        headers = ["G1 / a", "G1 / b", "G2 / c", "G2 / d"]
        rows = [["1", "2", "3", "4"]]
        windows, _ = build_column_windows(headers, rows, max_columns=PER_CHUNK)
        assert [w.group for w in windows] == ["G1", "G2"]
        assert [w.columns for w in windows] == [(0, 1), (2, 3)]

    def test_a_group_too_wide_is_split_and_said(self):
        headers = [f"G / c{i}" for i in range(20)]
        rows = [[str(i) for i in range(20)]]
        windows, warnings = build_column_windows(headers, rows, max_columns=PER_CHUNK)
        assert [len(w.columns) for w in windows] == [8, 8, 4]
        assert all(w.group == "G" for w in windows)
        assert "excel_wide_table_group_split" in warnings

    def test_ungrouped_neighbours_form_one_window(self):
        headers = ["a", "b", "c"]
        rows = [["1", "2", "3"]]
        windows, _ = build_column_windows(headers, rows, max_columns=PER_CHUNK)
        assert [w.columns for w in windows] == [(0, 1, 2)]


class TestIdentityColumns:
    def test_a_label_column_is_detected(self):
        rows = [["Common", "10"], ["Series A-1", "20"]]
        assert detect_identity_columns(["Security", "Shares"], rows) == (0,)

    def test_a_numeric_first_column_is_not_an_identity(self):
        """Dans le doute, RIEN. Une identification inventée attribuerait des chiffres
        à la mauvaise entité — pire que pas d'identification du tout."""
        rows = [["100", "10"], ["200", "20"]]
        assert detect_identity_columns(["Montant", "Shares"], rows) == ()

    def test_an_explicit_label_rescues_an_ambiguous_column(self):
        rows = [["2020", "10"], ["2021", "20"]]
        assert detect_identity_columns(["Series", "Shares"], rows) == (0,)

    def test_the_absence_of_identity_is_signalled(self):
        headers = ["a / x", "a / y"]
        rows = [["1", "2"]]
        _w, warnings = build_column_windows(headers, rows, max_columns=PER_CHUNK)
        assert "excel_wide_table_identity_column_missing" in warnings


class TestNarrowTablesAreUntouched:
    def test_a_narrow_table_keeps_the_historic_rendering(self):
        b = _block([["Projet", "Budget"], ["Atlas", 250], ["Orion", 120]])
        chunks, _ = _render(b)
        assert len(chunks) == 1
        m = chunks[0]["meta"]
        assert m.get("wide_table") is not True
        assert m["cell_range"] == "A2:B3"
        assert "| Atlas | 250 |" in chunks[0]["text"]

    def test_a_table_without_reliable_headers_is_not_split_blindly(self):
        """Sans en-tête, il n'y a ni groupe ni identité : découper serait deviner."""
        rows = [[i for i in range(30)] for _ in range(3)]
        b = _block(rows)
        assert b.headers_inferred is True
        chunks, _ = _render(b)
        assert all(c["meta"].get("wide_table") is not True for c in chunks)


class TestTheWideTableIsCutOnBothAxes:
    def test_each_chunk_carries_only_its_own_headers(self):
        chunks, warnings = _render(_block(_cap_table()))

        assert "excel_wide_table_column_chunking" in warnings
        assert len(chunks) >= 3

        groups = {c["meta"]["column_group"] for c in chunks}
        assert groups == {"Premoney", "AntiDil", "Postmoney"}

        post = next(c for c in chunks if c["meta"]["column_group"] == "Postmoney")
        # LE point du lot : les 12 intitulés ne sont plus tous là.
        assert "Issue Price" not in post["text"]
        assert "Premoney / Diluted" not in post["text"]
        assert "Postmoney / Shares" in post["text"]

    def test_the_identity_column_is_carried_into_every_window(self):
        chunks, _ = _render(_block(_cap_table()))
        for c in chunks:
            assert "Security" in c["meta"]["headers"]
            assert c["meta"]["identity_columns"] == ["A"]
            assert "Common" in c["text"]          # sans elle, « 300 » ne dit rien

    def test_the_ranges_never_lie(self):
        """La plage principale ne couvre PAS la colonne d'identification.

        L'agréger en « A3:L6 » désignerait des colonnes ABSENTES du chunk. On la cite
        donc séparément — la citation reste une preuve, pas une approximation.
        """
        chunks, _ = _render(_block(_cap_table()))
        post = next(c for c in chunks if c["meta"]["column_group"] == "Postmoney")
        m = post["meta"]

        assert m["cell_range"] == "N3:R6"
        assert m["data_range"] == "N3:R6"
        assert m["identity_range"] == "A3:A6"
        assert m["data_ranges"] == ["A3:A6", "N3:R6"]
        assert m["column_start"] == "N" and m["column_end"] == "R"
        assert m["header_range"] == "N1:R2"
        assert m["header_ranges"] == ["A1:A2", "N1:R2"]
        assert m["chunk_axis"] == "rows_and_columns"
        assert m["wide_table"] is True

    def test_empty_columns_are_never_rendered(self):
        chunks, _ = _render(_block(_cap_table()))
        for c in chunks:
            assert "col7" not in c["text"]
            assert "col13" not in c["text"]

    def test_the_chunk_count_is_bounded_and_the_truncation_is_said(self):
        chunks, warnings = _render(_block(_cap_table(n_rows=6)), max_chunks=2)
        assert len(chunks) == 2
        assert "excel_wide_table_chunk_limit_reached" in warnings

    def test_the_character_budget_is_respected(self):
        chunks, _ = _render(_block(_cap_table(n_rows=6)), chunk_max_chars=400)
        # Une ligne unique trop longue est conservée entière ; au-delà, on fenêtre.
        assert all(len(c["text"]) < 1200 for c in chunks)


class TestRedundancyActuallyDropped:
    def test_two_windows_share_almost_no_vocabulary(self):
        """La mesure qui compte : c'est la redondance qui cassait le retrieval."""
        chunks, _ = _render(_block(_cap_table()))
        pre = next(c for c in chunks if c["meta"]["column_group"] == "Premoney")
        post = next(c for c in chunks if c["meta"]["column_group"] == "Postmoney")

        def heads(c):
            return {h for h in c["meta"]["headers"]}

        common = heads(pre) & heads(post)
        # Seule la colonne d'identification est partagée — c'est le but.
        assert common == {"Security"}
