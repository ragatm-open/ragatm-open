"""contrats propriétaires des deux corrections.

Deux problèmes structurels génériques, qui n'ont rien de propre à un document :

**B1.** Une ligne peut se poursuivre sur la page suivante alors même que sa cellule
d'ancrage contient du texte — c'est la SUITE de cette cellule, coupée par la
pagination. Le signal est typographique et local au document : une cellule coupée
reprend sans espacement de paragraphe, une nouvelle ligne en ouvre un.

**B2.** Une matrice peut porter du contenu dans son coin tout en ayant des
hiérarchies de lignes et de colonnes indépendantes. Exiger un coin vide, comme le
faisait TABLE-1, est une hypothèse trop forte.

Aucun test n'emploie le vocabulaire du corpus de qualification.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.extraction import pdf_tables as P

from . import _pdf_table_fixtures as F

# --------------------------------------------------------------------------- #
# Aides
# --------------------------------------------------------------------------- #
def _tables(data: bytes):
    return P.extract_generic_tables(data).tables


def _one(data: bytes):
    ts = _tables(data)
    assert len(ts) == 1, f"un seul tableau logique attendu, obtenu {len(ts)}"
    return ts[0]


def _rows(table):
    return [r for r in table.rows if r.row_kind == P.ROW_KIND_DATA]


def _continued_rows(data: bytes):
    """Lignes logiques qui enjambent plusieurs pages.

    La continuation se rattache à la DERNIÈRE ligne de la page précédente, pas à la
    première du tableau : c'est elle que la pagination a coupée."""
    return [r for t in _tables(data) for r in _rows(t) if len(r.source_pages) > 1]


def _text(row) -> str:
    return " ".join(v for fr in row.fragments for v in fr.cells.values())


def _partition(data: bytes, page: int = 1):
    """(h, v) inférés pour le premier fragment de `page`."""
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        frag = P._collect_fragments(pdf, {page}, P.TableLimits())[0]
    model = P._build_header(frag, header_confirmed_by_repetition=False)
    return len(model.header_rows) if model.hierarchical else 0, model


def _matrix(spec) -> bytes:
    rows, spans = spec
    return F.matrix(rows, spans)


# =========================================================================== #
# B1 — continuation typographique
# =========================================================================== #
class TestB1NonBlankContinuation:
    """§19 : le cas que TABLE-1 ne savait pas traiter."""

    def test_anchor_carries_continuation_text(self):
        rows = _continued_rows(F.two_page_case(case="continuation", split_col=0))
        assert len(rows) == 1 and rows[0].source_pages == [1, 2], [r.source_pages for r in rows]

    def test_middle_column_split(self):
        rows = _continued_rows(F.two_page_case(case="continuation", split_col=1))
        assert len(rows) == 1 and rows[0].source_pages == [1, 2], [r.source_pages for r in rows]

    def test_last_column_split(self):
        rows = _continued_rows(F.two_page_case(case="continuation", split_col=3))
        assert len(rows) == 1 and rows[0].source_pages == [1, 2], [r.source_pages for r in rows]


class TestB1Boundaries:
    def test_continuation_then_new_row(self):
        data = F.two_page_case(case="cont_then_new")
        cont = _continued_rows(data)
        assert len(cont) == 1 and cont[0].source_pages == [1, 2]
        assert "genuinely new row" not in _text(cont[0]), _text(cont[0])
        others = [r for t in _tables(data) for r in _rows(t) if r is not cont[0]]
        assert any("genuinely new row" in _text(r) for r in others)

    def test_independent_table_is_not_joined(self):
        rows = [r for t in _tables(F.two_page_case(case="independent")) for r in _rows(t)]
        assert rows
        assert all(len(r.source_pages) == 1 for r in rows), [r.source_pages for r in rows]

    def test_blank_anchor_alone_is_still_not_continuation(self):
        """§18 : le garde-fou de TABLE-1 doit survivre à B1."""
        rows = _rows(_one(F.two_page_case(case="blank_anchor_new_row")))
        joined = [r for r in rows if len(r.source_pages) > 1]
        assert not joined, [r.source_pages for r in joined]


@pytest.mark.parametrize("kwargs,label", [
    ({"top": 12.0}, "marge haute 12"), ({"top": 48.0}, "marge haute 48"),
    ({"top": 72.0}, "marge haute 72"),
    ({"bottom": 12.0}, "marge basse 12"), ({"bottom": 72.0}, "marge basse 72"),
    ({"page": "Letter"}, "Letter"), ({"page": "Legal"}, "Legal"),
    ({"page": "Custom"}, "format personnalisé"),
    ({"orient": "portrait"}, "portrait"), ({"orient": "landscape"}, "paysage"),
    ({"fill_ratio": 0.35}, "tableau haut dans la page"),
    ({"fill_ratio": 0.85}, "tableau bas dans la page"),
    ({"delayed_top": True}, "reprise différée"),
    ({"size": 8.0}, "corps 8"), ({"size": 14.0}, "corps 14"),
    ({"leading": 10.0}, "interligne compact"), ({"leading": 22.0}, "interligne dilaté"),
    ({"space_before": 6.0}, "espacement 6"), ({"space_before": 14.0}, "espacement 14"),
])
class TestB1LayoutRobustness:
    """§24 : la décision ne doit dépendre ni de la page, ni de la fonte."""

    def test_continuation_detected(self, kwargs, label):
        rows = _continued_rows(F.two_page_case(case="continuation", **kwargs))
        assert len(rows) == 1 and rows[0].source_pages == [1, 2], \
            f"{label}: {[r.source_pages for r in rows]}"

    def test_independent_not_joined(self, kwargs, label):
        """Deux tableaux logiques ou un seul, peu importe : ce qui est interdit,
        c'est qu'une LIGNE enjambe les deux pages."""
        rows = [r for t in _tables(F.two_page_case(case="independent", **kwargs))
                for r in _rows(t)]
        assert rows, label
        assert all(len(r.source_pages) == 1 for r in rows), \
            f"{label}: {[r.source_pages for r in rows]}"


class TestB1Abstention:
    def test_small_sample_abstains(self):
        """§26 : trop peu de rangées mesurables — aucune coupe, repli TABLE-1."""
        assert P.calibrate_row_gap_cut([1.0, 2.0, 3.0]) is None

    def test_no_low_gap_population_abstains(self):
        """§27 : assez d'échantillons, mais aucune rangée ne commence plus haut."""
        assert P.calibrate_row_gap_cut([9.0] * 20) is None

    def test_cut_is_found_when_a_row_starts_higher(self):
        cut = P.calibrate_row_gap_cut([9.0] * 20 + [3.0])
        assert cut is not None and 3.0 <= cut < 9.0, cut

    def test_uniform_document_produces_no_cut(self):
        import io

        import pdfplumber

        data = F.uniform_gaps_table()
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            frags = P._collect_fragments(pdf, None, P.TableLimits())
            words = {p.page_number: (p.extract_words() or []) for p in pdf.pages}
        assert P.calibrate_row_gap_cut(P.document_row_gaps(frags, words)) is None


class TestB1RowGap:
    """§4-§7 : définition exacte de la mesure."""

    def _frag(self, data, page=1):
        import io

        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            frag = P._collect_fragments(pdf, {page}, P.TableLimits())[0]
            words = {p.page_number: (p.extract_words() or []) for p in pdf.pages}
        return frag, words

    def test_gap_is_the_minimum_over_non_empty_cells(self):
        """§5 : une seule cellule peut poursuivre un paragraphe ; la médiane
        classerait alors la rangée entière comme neuve."""
        frag, words = self._frag(F.two_page_case(case="continuation", split_col=1), page=2)
        gaps = P._cell_gaps(frag, words, 1)
        assert gaps, "aucun écart mesuré"
        assert P.row_gap(frag, words, 1) == min(gaps)

    def test_empty_cells_do_not_contribute(self):
        frag, words = self._frag(F.two_page_case(case="continuation", split_col=1), page=2)
        # la rangée n'a qu'UNE cellule remplie : un seul écart mesurable
        assert len(P._cell_gaps(frag, words, 1)) == 1

    def test_row_without_glyph_has_no_gap(self):
        frag, words = self._frag(F.flat_three_column())
        assert P.row_gap(frag, {}, 0) is None


class TestB1Separator:
    """§13-§15 : détection structurelle, sans aucun mot connu."""

    def test_multi_column_single_cell_row_is_a_separator(self):
        t = _one(F.separator_row())
        seps = [r for r in t.rows if r.row_kind == P.ROW_KIND_SEPARATOR]
        assert len(seps) == 1 and seps[0].row_label == F.SEPARATOR_LABEL

    def test_sparse_row_is_not_a_separator(self):
        t = _one(F.sparse_row())
        assert [r for r in t.rows if r.row_kind == P.ROW_KIND_SEPARATOR] == []

    def test_blank_cells_row_is_not_a_separator(self):
        t = _one(F.legitimate_blank_cells())
        assert [r for r in t.rows if r.row_kind == P.ROW_KIND_SEPARATOR] == []


# =========================================================================== #
# B2 — partition générique de matrice
# =========================================================================== #
@pytest.mark.parametrize("spec,h,v,label", [
    (F.MATRIX_EMPTY_CORNER, 2, 1, "coin vide"),
    (F.MATRIX_FILLED_CORNER, 2, 1, "coin rempli"),
    (F.MATRIX_MERGED_CORNER, 2, 1, "coin fusionné"),
    (F.MATRIX_MULTICELL_CORNER, 2, 1, "coin multi-cellules"),
    (F.MATRIX_H1_V1, 1, 1, "h=1 v=1"),
    (F.MATRIX_H2_V2, 2, 2, "h=2 v=2"),
    (F.MATRIX_H3_V2, 3, 2, "h=3 v=2, en-tête majoritaire"),
    (F.MATRIX_H4_V1, 4, 1, "h=4 v=1"),
    (F.MATRIX_TEXT_BODY, 2, 1, "corps texte"),
    (F.MATRIX_MIXED_BODY, 2, 1, "corps mixte"),
    (F.MATRIX_SPARSE_BODY, 2, 1, "corps épars"),
    (F.MATRIX_MULTILINE_BODY, 2, 1, "corps multi-lignes"),
    (F.MATRIX_IRREGULAR, 2, 1, "fusions irrégulières"),
    (F.MATRIX_MERGED_ROW_HEADER, 2, 1, "en-tête de lignes fusionné"),
])
class TestB2Partition:
    def test_depths(self, spec, h, v, label):
        _, model = _partition(_matrix(spec))
        assert model.hierarchical, f"{label}: matrice non reconnue"
        assert len(model.header_rows) == h, f"{label}: h={len(model.header_rows)}"
        assert len(model.row_header_intervals) == v, \
            f"{label}: v={len(model.row_header_intervals)}"

    def test_leaf_paths_are_unique(self, spec, h, v, label):
        _, model = _partition(_matrix(spec))
        paths = [tuple(p) for p in model.column_paths]
        assert len(set(paths)) == len(paths), f"{label}: chemins dupliqués {paths}"


@pytest.mark.parametrize("spec,label", [
    (F.FLAT_MERGED_TITLE, "rangée de titre fusionnée"),
    (F.FLAT_SUBTOTAL, "sous-total fusionné"),
    (F.FLAT_SECTION, "séparateur de section"),
    (F.FLAT_SPARSE, "tableau plat épars"),
    (F.FLAT_FILLED_CORNER, "coin supérieur gauche rempli"),
    (F.FLAT_MERGED_BODY_CELL, "cellule de corps fusionnée"),
])
class TestB2FlatNegatives:
    """§49 : une fusion ne fait pas une matrice."""

    def test_stays_flat(self, spec, label):
        _, model = _partition(_matrix(spec))
        assert not model.hierarchical, f"{label}: classé matrice à tort"


class TestB2UnevenHierarchy:
    def test_short_branch_keeps_its_depth(self):
        """§40 : aucune étiquette inventée pour égaliser les profondeurs."""
        _, model = _partition(_matrix(F.MATRIX_UNEVEN))
        lengths = sorted(len(p) for p in model.column_paths)
        assert lengths == [1, 2, 2], lengths


class TestB2MatrixValues:
    def test_row_and_column_paths_carry_the_value(self):
        t = _one(_matrix(F.MATRIX_FILLED_CORNER))
        assert t.table_type == P.TABLE_TYPE_MATRIX
        rows = _rows(t)
        assert [r.row_label for r in rows] == ["R1", "R2"]
        cells = rows[0].fragments[0].cells
        assert cells == {"Q1 > A": "1", "Q1 > B": "2", "Q2 > A": "3", "Q2 > B": "4"}

    def test_no_cross_axis_association(self):
        t = _one(_matrix(F.MATRIX_FILLED_CORNER))
        rows = _rows(t)
        assert rows[0].fragments[0].cells["Q1 > A"] != "4"
        assert rows[1].fragments[0].cells["Q2 > B"] == "8"
