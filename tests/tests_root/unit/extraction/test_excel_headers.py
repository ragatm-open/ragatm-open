"""P1.1 — en-têtes Excel multi-lignes, bandeaux fusionnés et « centrés sur sélection ».

Le défaut, découvert sur un vrai classeur financier : l'ancien détecteur ne regardait
que la PREMIÈRE ligne du bloc. Il prenait le bandeau (« Premoney Capitalization ») pour
l'en-tête — avec des trous `col1`, `col3` là où la fusion laisse des cellules vides — et
reléguait les VRAIS intitulés (« Shares Outstanding »…) au rang de ligne de données.

Deux mécanismes de propagation sont testés ici, et le second est une découverte du
terrain : le classeur réel ne contient **aucune cellule fusionnée**. Ses bandeaux sont
mis en forme par « Centrer sur plusieurs colonnes » — visuellement identiques, mais la
valeur ne vit que dans UNE cellule. Sans propagation par PORTÉE, seule la première
colonne du groupe héritait du bandeau.
"""
import pytest

from apps.rag_core.services.ingest.extraction.excel_blocks import Cell, split_sheet_into_blocks
from apps.rag_core.services.ingest.extraction.excel_headers import (
    CONFIDENCE_FALLBACK,
    CONFIDENCE_HIGH,
    HEADER_SEPARATOR,
    STRATEGY_MULTI_ROW,
    STRATEGY_MULTI_ROW_MERGED,
    STRATEGY_SINGLE_ROW,
    MergedHeaderMap,
    build_headers,
    detect_header_rows,
    normalize_header_label,
)


def _grid(rows):
    """`rows` : liste de listes de valeurs. Un nombre => cellule NUMÉRIQUE (donnée)."""
    out = []
    for r in rows:
        cells = []
        for v in r:
            if v is None or v == "":
                cells.append(Cell(text="", is_numeric=False))
            elif isinstance(v, (int, float)):
                cells.append(Cell(text=str(v), is_numeric=True))
            else:
                cells.append(Cell(text=str(v), is_numeric=False))
        out.append(cells)
    return out


def _headers(rows, *, merged=(), max_depth=4, first_row=1, first_col=1):
    cells = _grid(rows)
    width = max(len(r) for r in cells)
    m = MergedHeaderMap(merged, row_min=first_row, row_max=first_row + len(cells) - 1)
    det = detect_header_rows(
        cells, merged=m, first_row=first_row, first_col=first_col,
        width=width, max_depth=max_depth,
    )
    hs, warns = build_headers(
        cells, det, merged=m, first_row=first_row, first_col=first_col, width=width
    )
    return hs, det, warns


# --------------------------------------------------------------------------- #
# Le cas qui a motivé le lot
# --------------------------------------------------------------------------- #
class TestMergedBanner:
    def test_merged_banner_above_column_titles(self):
        """Le cas canonique : fusion `B1:D1` au-dessus des vrais intitulés."""
        hs, det, _ = _headers(
            [
                ["", "Premoney Capitalization", "", ""],
                ["Security", "Shares Outstanding", "Fully Diluted Shares", "Ownership %"],
                ["Common Stock", 1250000, 1500000, 0.625],
            ],
            merged=[(2, 1, 4, 1)],          # B1:D1
        )
        assert hs == [
            "Security",
            "Premoney Capitalization / Shares Outstanding",
            "Premoney Capitalization / Fully Diluted Shares",
            "Premoney Capitalization / Ownership %",
        ]
        assert det.header_row_indices == (0, 1)
        assert det.strategy == STRATEGY_MULTI_ROW_MERGED
        assert det.confidence == CONFIDENCE_HIGH
        assert det.headers_inferred is False

    def test_a_span_banner_without_any_merge(self):
        """🔴 LE cas du terrain : « Centrer sur plusieurs colonnes », AUCUNE fusion.

        La valeur ne vit que dans B1. Sans propagation par portée, C et D restaient
        orphelines — c'est ce qui produisait `Fully Diluted Shares` sans son groupe.
        """
        hs, det, _ = _headers([
            ["", "Premoney Capitalization", "", ""],
            ["Security", "Shares Outstanding", "Fully Diluted Shares", "Ownership %"],
            ["Common Stock", 1250000, 1500000, 0.625],
        ])                                   # merged=() : aucune fusion !
        assert hs[1] == "Premoney Capitalization / Shares Outstanding"
        assert hs[2] == "Premoney Capitalization / Fully Diluted Shares"
        assert hs[3] == "Premoney Capitalization / Ownership %"
        assert hs[0] == "Security"           # la colonne d'identification n'est PAS préfixée
        assert det.strategy == STRATEGY_MULTI_ROW

    def test_two_groups_side_by_side(self):
        hs, _, _ = _headers([
            ["", "Premoney Capitalization", "", "Postmoney Capitalization", ""],
            ["Security", "Shares", "Ownership %", "Shares", "Ownership %"],
            ["Common", 100, 0.5, 120, 0.6],
        ])
        assert hs == [
            "Security",
            "Premoney Capitalization / Shares",
            "Premoney Capitalization / Ownership %",
            "Postmoney Capitalization / Shares",
            "Postmoney Capitalization / Ownership %",
        ]

    def test_the_span_stops_at_an_empty_leaf_column(self):
        """Une colonne séparatrice (sans intitulé propre) n'est PAS absorbée par le groupe."""
        hs, _, _ = _headers([
            ["", "Groupe A", "", "", "Groupe B", ""],
            ["ID", "X", "Y", "", "Z", "W"],       # col D n'a pas d'intitulé feuille
            ["a", 1, 2, None, 3, 4],
        ])
        assert hs[1] == "Groupe A / X"
        assert hs[2] == "Groupe A / Y"
        assert hs[3] == "col4"                     # séparatrice : rien n'est inventé
        assert hs[4] == "Groupe B / Z"

    def test_the_identification_column_keeps_its_own_label(self):
        hs, _, _ = _headers([
            ["", "Financial Metrics", "", ""],
            ["Project", "Revenue", "EBITDA", "Margin"],
            ["Atlas", 100, 20, 0.2],
        ])
        assert hs[0] == "Project"
        assert hs[1] == "Financial Metrics / Revenue"


class TestDepth:
    def test_three_levels(self):
        hs, det, _ = _headers([
            ["", "Capitalization", "", "", ""],
            ["", "Premoney", "", "Postmoney", ""],
            ["Security", "Shares", "%", "Shares", "%"],
            ["Common", 100, 0.5, 120, 0.6],
        ])
        assert det.header_row_indices == (0, 1, 2)
        assert hs[1] == "Capitalization / Premoney / Shares"
        assert hs[4] == "Capitalization / Postmoney / %"
        assert hs[0] == "Security"

    def test_single_header_row_is_unchanged(self):
        """Non-régression : le tableau simple garde exactement son comportement."""
        hs, det, _ = _headers([
            ["Projet", "Budget", "Réalisé"],
            ["Atlas", 250000, 220000],
        ])
        assert hs == ["Projet", "Budget", "Réalisé"]
        assert det.header_row_indices == (0,)
        assert det.strategy == STRATEGY_SINGLE_ROW
        assert det.headers_inferred is False

    def test_depth_is_truncated_and_reported(self):
        """On ne consomme JAMAIS dix lignes comme en-tête."""
        rows = [[f"N{i}", f"L{i}"] for i in range(6)] + [["Atlas", 1]]
        hs, det, _ = _headers(rows, max_depth=2)
        assert len(det.header_row_indices) == 2
        # On garde les lignes les plus PROCHES des données (les plus spécifiques).
        assert det.header_row_indices == (4, 5)
        assert "excel_header_depth_truncated" in det.warnings


class TestTitleVsHeaderLevel:
    def test_a_standalone_title_is_not_absorbed(self):
        """« Financing Overview » seul au-dessus d'un tableau : c'est un TITRE."""
        hs, det, _ = _headers([
            ["Financing Overview", "", ""],
            ["Security", "Shares", "Price"],
            ["Common", 100, 1.5],
        ])
        assert det.title_row_index == 0
        assert hs == ["Security", "Shares", "Price"]
        assert not any("Financing Overview" in h for h in hs)

    def test_a_full_width_banner_is_a_title_not_a_prefix(self):
        """Un libellé couvrant TOUTE la largeur ne discrimine aucune colonne.

        Le préfixer partout n'ajouterait que du bruit (`X / a`, `X / b`, …).
        """
        hs, det, _ = _headers(
            [
                ["Cap Table", "", ""],
                ["Security", "Shares", "Price"],
                ["Common", 100, 1.5],
            ],
            merged=[(1, 1, 3, 1)],           # A1:C1, toute la largeur
        )
        assert det.title_row_index == 0
        assert hs == ["Security", "Shares", "Price"]

    def test_a_group_banner_is_a_level_not_a_title(self):
        _, det, _ = _headers([
            ["", "Premoney", "", ""],
            ["Security", "Shares", "FD Shares", "%"],
            ["Common", 1, 2, 0.5],
        ])
        assert det.title_row_index is None    # un GROUPE n'est pas un titre


class TestReconstruction:
    def test_adjacent_duplicates_are_collapsed(self):
        hs, _, _ = _headers([
            ["", "Revenue", ""],
            ["Year", "Revenue", "Growth"],
            [2026, 100, 0.1],
        ])
        assert hs[1] == "Revenue"             # PAS « Revenue / Revenue »
        assert HEADER_SEPARATOR not in hs[1]

    def test_duplicate_columns_are_disambiguated(self):
        hs, _, warns = _headers([
            ["", "Premoney", "", "Postmoney", ""],
            ["ID", "Shares", "Shares", "Shares", "Shares"],
            ["a", 1, 2, 3, 4],
        ])
        # Les deux « Premoney / Shares » sont distingués sans recourir à la lettre de colonne.
        assert hs[1] == "Premoney / Shares"
        assert hs[2] == "Premoney / Shares [2]"
        assert "excel_duplicate_headers_disambiguated" in warns

    def test_empty_levels_are_skipped(self):
        hs, _, _ = _headers([
            ["", "", "Financials", ""],
            ["Project", "", "Revenue", "EBITDA"],
            ["Atlas", None, 100, 20],
        ])
        assert hs[0] == "Project"
        assert hs[2] == "Financials / Revenue"


class TestFallback:
    def test_first_row_is_data_no_header(self):
        hs, det, _ = _headers([[1, 2, 3], [4, 5, 6]])
        assert det.headers_inferred is True
        assert det.confidence == CONFIDENCE_FALLBACK
        assert hs == ["col1", "col2", "col3"]

    def test_no_data_row_falls_back(self):
        """Aucune donnée typée : c'est du texte, pas un tableau à en-tête."""
        hs, det, _ = _headers([["a", "b"], ["c", "d"]])
        assert det.headers_inferred is True
        assert hs == ["col1", "col2"]

    def test_nothing_is_ever_invented(self):
        """L'invariant du lot : `col1` plutôt qu'un intitulé faux."""
        hs, det, _ = _headers([[1, 2], [3, 4]])
        assert all(h.startswith("col") for h in hs)
        assert det.headers_inferred is True


class TestLabelNormalization:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("Shares\nOutstanding", "Shares Outstanding"),
        ("  Fully   Diluted Shares ", "Fully Diluted Shares"),
        ("Ownership %", "Ownership %"),
        ("EBITDA", "EBITDA"),                 # acronyme : casse préservée
        ("ARR", "ARR"),
        ("FY2026", "FY2026"),
        ("Coût & marge", "Coût & marge"),     # symboles métier conservés
        ("Prix / unité", "Prix / unité"),
    ])
    def test_labels(self, raw, expected):
        assert normalize_header_label(raw) == expected

    def test_long_labels_are_bounded(self):
        assert len(normalize_header_label("x" * 500)) == 120

    def test_non_string_is_safe(self):
        assert normalize_header_label(None) == ""


# --------------------------------------------------------------------------- #
# Données : rien n'est absorbé, rien n'est propagé
# --------------------------------------------------------------------------- #
class TestDataIntegrity:
    def test_header_rows_never_become_data(self):
        blocks, _, _ = split_sheet_into_blocks(
            _grid([
                ["", "Premoney", "", ""],
                ["Security", "Shares", "FD", "%"],
                ["Common", 100, 120, 0.5],
                ["Series A", 200, 220, 0.5],
            ]),
            max_blocks=10,
        )
        b = blocks[0]
        assert b.header_rows == [1, 2]
        assert b.header_depth == 2
        assert len(b.rows) == 2                       # SEULES les 2 lignes de données
        assert b.rows[0][0] == "Common"
        # Ni « Premoney » ni « Security » ne se retrouvent en donnée.
        flat = [c for r in b.rows for c in r]
        assert "Premoney" not in flat and "Security" not in flat

    def test_the_merge_never_leaks_into_the_data(self):
        """Une fusion DANS les données ne doit propager aucune valeur."""
        blocks, _, _ = split_sheet_into_blocks(
            _grid([
                ["Security", "Shares"],
                ["Common", 100],
                ["Total", ""],                        # A3:B3 « fusionné » en donnée
            ]),
            max_blocks=10,
            merged_ranges=[(1, 3, 2, 3)],             # A3:B3
        )
        b = blocks[0]
        assert b.rows[-1] == ["Total", ""]            # B3 reste VIDE, jamais « Total »

    def test_coordinates_stay_exact(self):
        blocks, _, _ = split_sheet_into_blocks(
            _grid([
                ["", "Premoney", "", ""],
                ["Security", "Shares", "FD", "%"],
                ["Common", 100, 120, 0.5],
            ]),
            max_blocks=10,
            first_row=5,
        )
        b = blocks[0]
        assert b.header_rows == [5, 6]
        assert b.row_start == 5
        assert b.col_start == 1 and b.col_end == 4


class TestPerformance:
    def test_wide_multirow_header_is_linear(self):
        """256 colonnes, 4 lignes d'en-tête, des dizaines de fusions : borne LARGE.

        On protège contre le piège « pour chaque cellule, parcourir toutes les fusions »,
        pas contre une microseconde.
        """
        import time

        width = 256
        rows = [
            [f"G{j // 8}" if j % 8 == 0 else "" for j in range(width)],
            [f"S{j // 4}" if j % 4 == 0 else "" for j in range(width)],
            [f"c{j}" for j in range(width)],
            list(range(width)),
        ]
        merged = [(j + 1, 1, j + 8, 1) for j in range(0, width, 8)]

        t0 = time.perf_counter()
        hs, det, _ = _headers(rows, merged=merged)
        elapsed = time.perf_counter() - t0

        assert len(hs) == width
        assert det.header_row_indices == (0, 1, 2)
        assert elapsed < 1.0, f"{elapsed:.3f}s — coût probablement quadratique"
