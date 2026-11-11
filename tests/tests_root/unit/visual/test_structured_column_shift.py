"""garde-fou d'association colonne / valeur.

Constat réel (staging, bilan de puissance, PAGE-TABLE-ROUTING-1) : le moteur fusionne plusieurs
libellés de lignes dans quelques cellules ; sur les lignes qui ont perdu leur libellé, la
première valeur occupe la colonne des libellés et chaque valeur suivante se trouve sous
l'en-tête de sa voisine de gauche. 162 valeurs publiées sous la mauvaise colonne.

``REAL_SHAPE`` reproduit la FORME de cette grille (54 × 8, fusions, lignes vides omises), cellule
par cellule, avec des libellés et des valeurs synthétiques : aucune donnée du document.
Codes : ``T<n>`` texte de n mots, ``T#<n>`` texte avec chiffres, ``N`` un nombre, ``N<k>``
k nombres dans une cellule, ``.`` vide.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.visual import structured as S

W, H = 1184, 1673
REGION = (59, 135, 1118, 1404)


class _Asset:
    content_hash = "b" * 64
    ordinal = 0
    source_page = 1
    source_locator = None


def _cell(r, c, text, rs=1, cs=1):
    return {"row": r, "col": c, "row_span": rs, "col_span": cs, "text": text}


def _response(rows, *, extra_cells=(), n_rows=None, items=None):
    """Réponse ``structured-extract/1`` : ``rows`` = textes par position (``None`` = pas d'ancre)."""
    def item(rows, extra_cells=(), n_rows=None):
        cells = [_cell(r, c, t) for r, row in enumerate(rows) for c, t in enumerate(row) if t is not None]
        cells += list(extra_cells)
        return {"region_bbox": list(REGION), "n_rows": n_rows or len(rows), "n_cols": max(len(r) for r in rows),
                "headers": [], "rows": [], "cells": cells, "has_merged_cells": bool(extra_cells), "html": ""}
    box = [REGION[0] - 2, REGION[1] - 2, REGION[2] + 2, REGION[3] + 2]
    return {"task": "table", "contract": "structured-extract/1", "experimental": True,
            "qualified_for_production": False, "extraction_status": "success", "warnings": [],
            "image": {"width": W, "height": H},
            "items": items or [item(rows, extra_cells, n_rows)],
            "raw_output": {"layout_boxes": [{"label": "table", "score": 0.97, "coordinate": box}],
                           "ocr": {"rec_texts": [], "rec_scores": [], "rec_boxes": []}}}


def _norm(data):
    return S.normalize_response(data, asset=_Asset(), task="table", filename="bilan.pdf", response_sha256="c" * 64)


def _verdict(data):
    norm = _norm(data)
    return S.publication_refusal(data, task="table", norm=norm), norm


@pytest.fixture(autouse=True)
def _staging(settings):
    settings.DJANGO_ENV = "staging"
    settings.STRUCTURED_VISUAL_ALLOWED_TASKS = []


# --------------------------------------------------------------------------- #
# Forme réelle anonymisée
# --------------------------------------------------------------------------- #
REAL_SHAPE = """
T1 T3 T1 T3 T1 T2 T3 T2
T#5 . . . . . . .
T#8 . N N N N N .
N N N N N . . .
T#15 N N N N N . .
N N N N . . . .
N N N N N N . .
N N N N N . . .
T#11 . . . . . . .
N N N N N . . .
N3 N N N N . . .
T9 N N N N . . .
N N N N6 N N . .
N N N N N . . .
N N N N N . . .
N3 N N N N . . .
T15 . . . . . . .
N N N N . . . .
N N N N N . . .
N N N N N . . .
T19 N N N N N . .
N N N N N N . .
N N N N N . . .
T#22 . . . . N2 . .
N N N N N . . .
N N N N N . . .
N N N N N N . .
N N N N N . . .
N2 N N N N . . .
N N N N . . . .
N N N N N . . .
T#22 . . . . . . .
N N N N N . . .
T#3 N N N N N . .
N N N N N . . .
N N N N N . . .
N N N N N . . .
N N N N N N . .
T#13 N N N N N . .
T1 N N N N N . .
N N N N N . . .
N N N N N N . .
T#3 N N N N N . .
T#16 N N N N N . .
N N N N N . . .
N N N N N . . .
N N N N N . . .
N3 N N N N N . .
"""
#: Fusions relevées (ligne, colonne, rangées, colonnes, texte présent) ; 6 lignes vides omises.
REAL_MERGED = [(1, 0, 1, 8, True), (2, 6, 1, 2, True), (3, 6, 1, 2, False)]
REAL_N_ROWS = 54


def _synthetic(code: str, r: int, k: int) -> str:
    if code == ".":
        return ""
    if code == "N":
        return f"{(r * 37 + k * 11) % 900 + 1}.{(r + k) % 10}0"
    if code.startswith("N"):
        return " ".join(f"{r + i}.{k}5" for i in range(int(code[1:])))
    words = int(code.lstrip("T#"))
    base = ["Poste"] + [f"élément{r}x{k}x{i}" for i in range(words - 1)]
    if code.startswith("T#"):
        base[-1] = f"{base[-1]} {r + k}"
    return " ".join(base)


def real_shape_response():
    rows = [line.split() for line in REAL_SHAPE.strip().splitlines()]
    texts = [[_synthetic(code, r, k) for k, code in enumerate(row)] for r, row in enumerate(rows)]
    for r, c, rs, cs, has_text in REAL_MERGED:  # ancre fusionnée, positions couvertes sans ancre
        for k in range(c + 1, c + cs):
            texts[r][k] = None
        if not has_text:
            texts[r][c] = None
    extra = [_cell(r, c, _synthetic("T#5", r, c) if has_text else "", rs, cs)
             for r, c, rs, cs, has_text in REAL_MERGED]
    for r, c, *_ in REAL_MERGED:
        texts[r][c] = None
    return _response(texts, extra_cells=extra, n_rows=REAL_N_ROWS)


class TestRealFailureReplay:
    def test_real_shape_is_refused_before_any_fragment(self):
        refusal, norm = _verdict(real_shape_response())
        assert refusal == S.REFUSAL_COLUMN_ASSOCIATION_UNSAFE
        assert norm.refusal == (S.REFUSAL_COLUMN_ASSOCIATION_UNSAFE, "row_values_shifted_into_label_column")
        assert norm.tables == [] and norm.units == []

    def test_without_the_guard_the_shifted_rows_were_published(self, monkeypatch):
        """Ligne de base : la même grille passait toutes les barrières existantes."""
        monkeypatch.setattr(S, "column_shift", lambda *a, **k: None)
        data = real_shape_response()
        refusal, norm = _verdict(data)
        assert refusal is None
        assert len(norm.tables) == 46 and norm.units  # staging : 46 fragments de ligne publiés et indexés

    def test_signal_counts_on_the_real_shape(self):
        data = real_shape_response()
        anchors, cover = S._cell_grid(data["items"][0])
        sections, _, _ = S.row_groups(anchors, cover, REAL_N_ROWS, 8, 1)
        headed = [r for r in range(1, REAL_N_ROWS) if 0 in S._numeric_columns(anchors, r, 8)
                  and len(S._numeric_columns(anchors, r, 8)) >= 2]
        assert sections == {1} and len(headed) == 33


# --------------------------------------------------------------------------- #
# Scénarios adversariaux et tableaux valides
# --------------------------------------------------------------------------- #
HEAD = ["Équipement", "Puissance (kW)", "Tension (V)", "Intensité (A)"]


class TestShiftScenarios:
    def test_shift_01_fused_labels_then_label_less_rows(self):
        rows = [HEAD, ["Pompe A Pompe B Pompe C", "18", "400", "32"],
                ["24", "400", "43", ""], ["30", "400", "54", ""], ["Ventilateur", "5", "230", "22"]]
        assert _verdict(_response(rows))[0] == S.REFUSAL_COLUMN_ASSOCIATION_UNSAFE

    def test_shift_02_numbers_moved_into_the_label_column(self):
        rows = [HEAD, ["Pompe A", "18", "400", "32"], ["24", "400", "43", ""], ["7,5", "230", "33", ""]]
        assert _verdict(_response(rows))[0] == S.REFUSAL_COLUMN_ASSOCIATION_UNSAFE

    def test_shift_03_cell_outside_the_declared_grid(self):
        rows = [HEAD, ["Pompe A", "18", "400", "32"]]
        data = _response(rows, extra_cells=[_cell(1, 4, "99")])
        data["items"][0]["n_cols"] = 4
        assert _verdict(data)[0] == S.REFUSAL_GRID_INVALID
        assert _norm(data).refusal == (S.REFUSAL_GRID_INVALID, "cell_outside_declared_grid")

    def test_shift_04_header_incompatible_with_cells(self):
        """Fusion d'en-tête qui ne dit pas quelles colonnes elle couvre : refus existant."""
        rows = [[None, None, "Tension (V)", "Intensité (A)"], ["Pompe A", "18", "400", "32"],
                ["Pompe B", "24", "400", "43"]]
        data = _response(rows, extra_cells=[_cell(0, 0, "Équipement et puissance", cs=2)])
        assert _verdict(data)[0] == S.REFUSAL_COMPLEX_HEADER

    def test_shift_05_numeric_identifiers_with_total_row_are_published(self):
        rows = [["Année", "Logements", "Surface (m²)", "Montant"],
                ["2021", "120", "8 400", "1 250,5"], ["2022", "131", "9 020", "1 310,0"],
                ["2023", "140", "9 610", "1 402,7"], ["2024", "152", "", ""],
                ["Total", "543", "27 030", "3 963,2"]]
        refusal, norm = _verdict(_response(rows))
        assert refusal is None and len(norm.tables) == 5

    def test_shift_05b_equipment_codes_in_the_label_column(self):
        rows = [["Repère", "Puissance (kW)", "Tension (V)", "Intensité (A)"],
                ["101", "18", "400", "32"], ["102", "24", "400", "43"], ["103", "30", "400", "54"],
                ["TD-04", "5", "230", "22"]]
        assert _verdict(_response(rows))[0] is None

    def test_shift_06_merged_cells_are_published(self):
        rows = [HEAD, [None, "18", "400", "32"], [None, "24", "400", "43"], [None, None, None, None],
                ["Éclairage", "2", "230", "9"]]
        data = _response(rows, extra_cells=[_cell(1, 0, "Pompes", rs=2), _cell(3, 0, "Hors tension", cs=4)])
        refusal, norm = _verdict(data)
        assert refusal is None
        assert sum("Pompes" in t["markdown"] for t in norm.tables) == 2

    def test_shift_07_three_level_header_is_published(self):
        rows = [[None, None, None, None, None], [None, None, None, None, None],
                [None, "Hiver", "Été", "Hiver", "Été"],
                ["Bâtiment A", "12", "8", "40", "35"], ["Bâtiment B", "15", "9", "44", "37"]]
        extra = [_cell(0, 0, "Zone", rs=3), _cell(0, 1, "Consommation", cs=4),
                 _cell(1, 1, "Électricité (MWh)", cs=2), _cell(1, 3, "Gaz (MWh)", cs=2)]
        refusal, norm = _verdict(_response(rows, extra_cells=extra))
        assert refusal is None and len(norm.tables) == 2
        assert "Consommation / Gaz (MWh) / Été" in norm.tables[0]["markdown"]

    def test_shift_08_section_rows_are_published(self):
        rows = [HEAD, [None] * 4, ["Pompe A", "18", "400", "32"], [None] * 4, ["Pompe A", "11", "400", "20"]]
        extra = [_cell(1, 0, "Niveau 1", cs=4), _cell(3, 0, "Niveau 2", cs=4)]
        refusal, norm = _verdict(_response(rows, extra_cells=extra))
        assert refusal is None and len(norm.tables) == 2

    def test_shift_09_negative_values_decimals_and_units_are_preserved(self):
        rows = [["Poste", "Écart (%)", "Montant (€)", "Variation"],
                ["Énergie", "-6,2", "1 234,56", "−3.40"], ["Eau", "+0,4", "98,10", "12 %"],
                ["Chauffage", "-12,5", "-2 040,00", "-0,8 %"]]
        refusal, norm = _verdict(_response(rows))
        assert refusal is None
        text = "\n".join(t["markdown"] for t in norm.tables)
        for value in ("-6,2", "1 234,56", "−3.40", "-2 040,00", "-0,8 %", "12 %"):
            assert value in text

    def test_shift_10_coherent_shift_without_signal_is_a_documented_limit(self):
        """TOUTES les lignes ont perdu leur libellé : aucune ligne de référence, rien de détectable
        dans la grille seule. Le garde-fou ne prétend pas le voir (limite documentée)."""
        rows = [HEAD, ["18", "400", "32", ""], ["24", "400", "43", ""], ["30", "400", "54", ""]]
        assert _verdict(_response(rows))[0] is None

    def test_single_shifted_row_is_below_the_threshold(self):
        """Une seule ligne à nombre en tête qui « ressemble » à un décalage : donnée manquante
        plausible, aucun refus (seuil COLUMN_SHIFT_MIN_ROWS)."""
        rows = [HEAD, ["Pompe A", "18", "400", "32"], ["Pompe B", "24", "400", "43"], ["30", "400", "54", ""]]
        assert _verdict(_response(rows))[0] is None

    def test_shifted_rows_minority_among_numeric_rows_is_not_refused(self):
        """Tableau d'années : deux années récentes incomplètes ne font pas un décalage."""
        rows = [["Année", "T1", "T2", "T3"], ["2019", "1", "2", "3"], ["2020", "1", "2", "3"],
                ["2021", "1", "2", "3"], ["2022", "1", "2", "3"], ["2023", "1", "2", "3"],
                ["2024", "4", "5", ""], ["2025", "4", "5", ""], ["Moyenne", "2", "3", "3"],
                ["Écart", "1", "1", ""]]
        assert _verdict(_response(rows))[0] is None

    def test_refusal_covers_every_table_of_the_response(self):
        valid = [HEAD, ["Pompe A", "18", "400", "32"], ["Pompe B", "24", "400", "43"]]
        shifted = [HEAD, ["Pompe A", "18", "400", "32"], ["24", "400", "43", ""], ["30", "400", "54", ""]]

        def item(rows):
            return {"region_bbox": list(REGION), "n_rows": len(rows), "n_cols": 4, "headers": [], "rows": [],
                    "cells": [_cell(r, c, t) for r, row in enumerate(rows) for c, t in enumerate(row)],
                    "has_merged_cells": False, "html": ""}
        refusal, norm = _verdict(_response(None, items=[item(valid), item(shifted)]))
        assert refusal == S.REFUSAL_COLUMN_ASSOCIATION_UNSAFE

    def test_guard_never_rewrites_cells(self):
        rows = [HEAD, ["Pompe A", "18", "400", "32"], ["24", "400", "43", ""], ["30", "400", "54", ""]]
        data = _response(rows)
        before = [dict(c) for c in data["items"][0]["cells"]]
        _verdict(data)
        assert data["items"][0]["cells"] == before
