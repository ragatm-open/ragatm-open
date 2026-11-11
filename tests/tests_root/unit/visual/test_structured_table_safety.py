"""sûreté de publication des tableaux structurés.

Fixtures SYNTHÉTIQUES au format réel de ``structured-extract/1`` (Gateway V1 :
``raw_output.layout_boxes`` avec ``coordinate``, ``region_bbox`` = boîte ``table``) :
  * grille incohérente (doublon, recouvrement, hors grille, coordonnée ou span invalide,
    taille démesurée) : refus, jamais de réparation ;
  * graphique pris pour un tableau : refus quand le moteur ne voit AUCUNE boîte ``table``
    sous l'élément, même si la réponse porte des cellules ; preuve absente = refus (lot 4) ;
  * en-tête hiérarchique : chemins complets (valeur <-> niveaux d'en-tête) quand la forme est
    sans ambiguïté, refus explicite sinon, jamais d'aplatissement qui change le sens ;
  * cellule fusionnée sur plusieurs lignes : qualifie chaque ligne couverte ;
  * tableaux valides : toujours publiés, marqueurs expérimentaux conservés.
"""
from __future__ import annotations

import copy

import pytest

from apps.rag_core.services.ingest.visual import structured as S

MONTHS = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]


class _Asset:
    content_hash = "e" * 64
    ordinal = 0
    source_page = 4
    source_locator = None


def _cell(r, c, text, rs=1, cs=1):
    return {"row": r, "col": c, "row_span": rs, "col_span": cs, "text": text}


def _grid(rows):
    return [_cell(r, c, t) for r, row in enumerate(rows) for c, t in enumerate(row)]


def _response(cells, n_rows, n_cols, *, region=(40, 40, 760, 340), boxes=(("table", (39, 39, 761, 341)),)):
    return {
        "task": "table", "contract": "structured-extract/1", "experimental": True,
        "qualified_for_production": False, "extraction_status": "success", "warnings": [],
        "image": {"width": 1600, "height": 1200},
        "items": [{"region_bbox": list(region), "n_rows": n_rows, "n_cols": n_cols, "headers": [], "rows": [],
                   "cells": cells, "has_merged_cells": any(c["row_span"] > 1 or c["col_span"] > 1 for c in cells),
                   "html": "<table></table>"}],
        "raw_output": {"layout_boxes": [{"label": label, "score": 0.95, "coordinate": list(box)}
                                        for label, box in boxes]},
    }


MONTHLY = _response(_grid([[""] + MONTHS,
                           ["Eau (m3)"] + [str(10 * k) for k in range(1, 13)],
                           ["Gaz (MWh)"] + [str(v) for v in (31, 29, 27, 22, 18, 12, 9, 8, 14, 21, 26, 33)]]), 3, 13)

THREE_LEVEL = _response(
    [_cell(0, 0, "Site", rs=3), _cell(0, 1, "Consommation 2025 (MWh)", cs=6),
     _cell(1, 1, "S1", cs=3), _cell(1, 4, "S2", cs=3)]
    + [_cell(2, 1 + k, m) for k, m in enumerate(["Jan", "Fév", "Mar", "Juil", "Août", "Sep"])]
    + _grid([[], [], [], ["Lyon", "12", "15", "11", "7", "6", "9"], ["Nantes", "21", "19", "23", "14", "13", "17"]]),
    5, 7)

MERGED_ROWS = _response(
    [_cell(0, 0, "Catégorie"), _cell(0, 1, "Article"), _cell(0, 2, "Prix (€)"),
     _cell(1, 0, "Énergie", rs=2), _cell(1, 1, "Fioul"), _cell(1, 2, "1 200"),
     _cell(2, 1, "Gaz"), _cell(2, 2, "880"),
     _cell(3, 0, "Total général", cs=2), _cell(3, 2, "2 080")], 4, 3)


def _norm(data):
    return S.normalize_response(data, asset=_Asset(), task="table", filename="t.pdf", response_sha256="c" * 64)


def _refusal(data):
    return S.publication_refusal(data, task="table", norm=_norm(data))


def _rows(data):
    return [t["markdown"] for t in _norm(data).tables]


@pytest.fixture(autouse=True)
def _staging(settings):
    settings.DJANGO_ENV = "staging"
    settings.STRUCTURED_VISUAL_ALLOWED_TASKS = []


# =========================================================================== #
# Contrôles positifs : les tableaux valides restent publiés
# =========================================================================== #
class TestValidTablesStillPublished:
    def test_monthly_table_keeps_each_value_under_its_month(self):
        assert _refusal(MONTHLY) is None
        rows = _rows(MONTHLY)
        assert any("Jan:\n10" in r and "Juin:\n60" in r and "Déc:\n120" in r for r in rows)
        assert any("Août:\n8" in r for r in rows)

    def test_markers_stay_experimental(self):
        norm = _norm(MONTHLY)
        marks = norm.units[0].meta["visual"][S.STRUCTURED_KEY]
        assert marks["experimental"] is True and marks["qualified_for_production"] is False
        assert all(t["meta"]["requires_visual_check"] is True for t in norm.tables)

    def test_response_without_any_layout_box_is_refused(self):
        """Preuve ABSENTE (2/113 réponses staging historiques) : refus depuis NO-LAYOUT-FAIL-CLOSED-4
        (avant : publiée). Volontaire : aucune preuve positive de région tableau."""
        data = copy.deepcopy(MONTHLY)
        data["raw_output"] = {"layout_boxes": []}
        assert _refusal(data) == S.REFUSAL_REGION_UNPROVEN and _rows(data) == []


# =========================================================================== #
# E1 — grille incohérente : refus, jamais de réparation
# =========================================================================== #
class TestGridValidation:
    @pytest.mark.parametrize("mutate,reason", [
        (lambda c: c.append(dict(c[15], text="999")), "duplicate_or_overlapping_cell"),
        (lambda c: next(x for x in c if (x["row"], x["col"]) == (2, 4)).update(col_span=2),
         "duplicate_or_overlapping_cell"),
        (lambda c: c.append(_cell(1, 13, "77")), "cell_outside_declared_grid"),
        (lambda c: c[5].update(row=-1), "invalid_coordinate"),
        (lambda c: c[5].update(col="2"), "invalid_coordinate"),
        (lambda c: c[5].update(col_span=0), "invalid_coordinate"),
        (lambda c: c[5].update(row=True), "invalid_coordinate"),
    ])
    def test_inconsistent_grid_is_refused(self, mutate, reason):
        data = copy.deepcopy(MONTHLY)
        mutate(data["items"][0]["cells"])
        assert S.table_grid_problem(data["items"][0]) == reason
        assert _refusal(data) == S.REFUSAL_GRID_INVALID
        assert _norm(data).refusal == (S.REFUSAL_GRID_INVALID, reason)

    def test_unbounded_declared_size_is_refused_without_walking_it(self):
        data = copy.deepcopy(MONTHLY)
        data["items"][0]["n_rows"] = 10 ** 9
        assert _refusal(data) == S.REFUSAL_GRID_INVALID
        assert _norm(data).tables == []

    def test_missing_spans_mean_one(self):
        data = copy.deepcopy(MONTHLY)
        for cell in data["items"][0]["cells"]:
            del cell["row_span"], cell["col_span"]
        assert _refusal(data) is None

    def test_one_bad_table_blocks_the_whole_response(self):
        data = copy.deepcopy(MONTHLY)
        bad = copy.deepcopy(data["items"][0])
        bad["cells"].append(dict(bad["cells"][15]))
        data["items"].append(bad)
        norm = _norm(data)
        assert S.publication_refusal(data, task="table", norm=norm) == S.REFUSAL_GRID_INVALID


# =========================================================================== #
# E4 — graphique pris pour un tableau
# =========================================================================== #
class TestChartTakenForTable:
    CHART_BOX = (38, 51, 1371, 766)

    def test_cells_inside_a_chart_region_are_refused(self):
        """Réponse NON vide : l'absence accidentelle de cellules n'est pas la protection."""
        data = _response(_grid([["", "Jan", "Fév"], ["t", "42", "55"]]), 2, 3, region=self.CHART_BOX,
                         boxes=(("chart", self.CHART_BOX), ("figure_title", (560, 18, 836, 46))))
        assert _refusal(data) == S.REFUSAL_REGION_UNPROVEN
        assert _norm(data).tables == [] and _norm(data).units == []

    def test_mixed_image_publishes_the_table_region_only(self):
        """Constat (SAFE_MIXED_01) : tableau sur sa boîte ``table``, graphique à côté."""
        data = _response(_grid([["Trimestre", "Ventes (k€)"], ["T1", "310"], ["T2", "355"]]), 3, 2,
                         region=(38, 37, 536, 279),
                         boxes=(("chart", (48, 389, 1323, 1043)), ("table", (38.98, 37.6, 536.3, 279.9))))
        assert _refusal(data) is None
        assert any("Ventes (k€):\n310" in r for r in _rows(data))

    def test_table_item_placed_on_the_chart_box_is_refused(self):
        data = _response(_grid([["Trimestre", "Ventes (k€)"], ["T1", "310"]]), 2, 2, region=(48, 389, 1323, 1043),
                         boxes=(("chart", (48, 389, 1323, 1043)), ("table", (38, 37, 536, 279))))
        assert _refusal(data) == S.REFUSAL_REGION_UNPROVEN

    def test_item_without_region_while_layout_exists_is_refused(self):
        data = copy.deepcopy(MONTHLY)
        data["items"][0]["region_bbox"] = None
        assert _refusal(data) == S.REFUSAL_REGION_UNPROVEN

    def test_text_alone_never_decides(self):
        """Des chiffres partout ne font pas un graphique : seule la géométrie du moteur décide."""
        assert S.table_region_proof(MONTHLY["items"][0], MONTHLY) is None


# =========================================================================== #
# E2 / E3 — en-têtes hiérarchiques et cellules fusionnées
# =========================================================================== #
class TestHierarchicalHeaders:
    def test_each_value_carries_its_full_header_path(self):
        assert _refusal(THREE_LEVEL) is None
        rows = _rows(THREE_LEVEL)
        assert len(rows) == 2, "les rangées d'en-tête ne sont jamais publiées comme des données"
        lyon = next(r for r in rows if "Site:\nLyon" in r)
        assert "Consommation 2025 (MWh) / S1 / Jan:\n12" in lyon
        assert "Consommation 2025 (MWh) / S2 / Juil:\n7" in lyon
        assert "Consommation 2025 (MWh) / S2 / Sep:\n9" in lyon
        overview = _norm(THREE_LEVEL).units[0]
        assert "6 = Consommation 2025 (MWh) / S2 / Août" in overview.text
        assert overview.meta["visual"][S.STRUCTURED_KEY]["header_rows"] == 3

    def test_grouped_header_without_a_spanning_corner_is_refused(self):
        data = copy.deepcopy(THREE_LEVEL)
        cells = data["items"][0]["cells"]
        cells[0]["row_span"] = 1
        cells += [_cell(1, 0, ""), _cell(2, 0, "")]
        assert _refusal(data) == S.REFUSAL_COMPLEX_HEADER

    def test_repeated_header_block_below_is_refused(self):
        data = copy.deepcopy(THREE_LEVEL)
        for cell in data["items"][0]["cells"]:
            if cell["row"] == 4:
                cell["text"] = ["Nantes", "Oct", "Nov", "Déc", "Avr", "Mai", "Juin"][cell["col"]]
        assert _refusal(data) == S.REFUSAL_COMPLEX_HEADER

    def test_header_cell_overflowing_into_data_is_refused(self):
        data = copy.deepcopy(THREE_LEVEL)
        for cell in data["items"][0]["cells"]:
            if (cell["row"], cell["col"]) == (2, 3):
                cell["row_span"] = 2
            if (cell["row"], cell["col"]) == (3, 3):
                cell["text"] = ""
        data["items"][0]["cells"] = [c for c in data["items"][0]["cells"] if (c["row"], c["col"]) != (3, 3)]
        assert _refusal(data) == S.REFUSAL_COMPLEX_HEADER

    def test_full_width_title_row_stays_neutral(self):
        """Constat staging p36 (lot 2) inchangé : un titre pleine largeur n'est pas un en-tête."""
        data = _response([_cell(0, 0, "Registre EAU", cs=3)] + _grid([[], ["", "Janvier", "Février"],
                                                                       ["Chantier", "3,45", "13,4"]])[0:],
                         3, 3)
        assert _refusal(data) is None
        assert any("Column 2:\n3,45" in r for r in _rows(data))


class TestMergedCells:
    def test_row_spanning_label_qualifies_every_covered_row(self):
        rows = _rows(MERGED_ROWS)
        gaz = next(r for r in rows if "Article:\nGaz" in r)
        assert "Catégorie:\nÉnergie" in gaz and "Prix (€):\n880" in gaz

    def test_column_spanning_cell_is_written_once(self):
        total = next(r for r in _rows(MERGED_ROWS) if "Total général" in r)
        assert total.count("Total général") == 1 and "Article:" not in total
        assert "Prix (€):\n2 080" in total


# =========================================================================== #
# Limite assumée : un décalage COHÉRENT n'est pas détectable dans la sortie seule
# =========================================================================== #
def test_coherent_column_shift_is_not_claimed_as_detected():
    """Rotation des mois d'un cran, grille parfaite : publiée, et c'est une LIMITE documentée
    (docs/10-reference/limites-connues.md), pas une garantie. Marqueurs expérimentaux tenus."""
    data = copy.deepcopy(MONTHLY)
    header = sorted((c for c in data["items"][0]["cells"] if c["row"] == 0), key=lambda c: c["col"])
    for cell, text in zip(header, [""] + MONTHS[1:] + ["Jan"]):
        cell["text"] = text
    assert _refusal(data) is None
    norm = _norm(data)
    assert norm.units[0].meta["visual"][S.STRUCTURED_KEY]["qualified_for_production"] is False
    assert all(t["meta"]["requires_visual_check"] is True for t in norm.tables)
