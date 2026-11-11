"""preuve POSITIVE de région tableau avant publication.

Réponses au format réel de ``structured-extract/1`` (Gateway V1, fixtures SAFE_* du lot
TABLE-SAFETY-3) : ``image.width/height`` = repère du visuel traité ; ``raw_output.layout_boxes``
(``label``, ``coordinate`` en pixels de ce visuel) ; ``items[].region_bbox`` = la boîte ``table``.

Un tableau non vide n'est publié que si une boîte ``table`` bien formée, finie, non dégénérée et
contenue dans le visuel couvre sa région (elle-même valide et dans le visuel). Mise en page
absente, vide, mal formée, sans boîte ``table``, géométrie inexploitable ou hors repère : refus
(``structured_visual_table_region_unproven``), jamais de repli permissif. ``task=table`` ne
prouve rien.
"""
from __future__ import annotations

import copy
import math

import pytest

from apps.rag_core.services.ingest.visual import structured as S

W, H = 1600, 380
TABLE_BOX = [38.91, 39.73, 748.29, 339.82]
REGION = [38, 39, 748, 339]


class _Asset:
    content_hash = "d" * 64
    ordinal = 0
    source_page = 2
    source_locator = None


def _cell(r, c, text, rs=1, cs=1):
    return {"row": r, "col": c, "row_span": rs, "col_span": cs, "text": text}


SIMPLE = [_cell(r, c, t) for r, row in enumerate([["Poste", "Budget (k€)", "Réalisé (k€)"],
                                                  ["Énergie", "125", "118"], ["Eau", "34", "31"]])
          for c, t in enumerate(row)]
HIERARCHICAL = ([_cell(0, 0, "Site", rs=3), _cell(0, 1, "Consommation 2025 (MWh)", cs=2),
                 _cell(1, 1, "S1"), _cell(1, 2, "S2"), _cell(2, 1, "Jan"), _cell(2, 2, "Juil")]
                + [_cell(3, c, t) for c, t in enumerate(["Lyon", "12", "7"])]
                + [_cell(4, c, t) for c, t in enumerate(["Nantes", "21", "14"])])
MERGED = [_cell(0, 0, "Catégorie"), _cell(0, 1, "Article"), _cell(0, 2, "Prix (€)"),
          _cell(1, 0, "Énergie", rs=2), _cell(1, 1, "Fioul"), _cell(1, 2, "1 200"),
          _cell(2, 1, "Gaz"), _cell(2, 2, "880")]


def _response(cells, n_rows, n_cols, *, region=REGION, boxes=(("table", TABLE_BOX),), width=W, height=H,
              raw="default"):
    data = {
        "task": "table", "contract": "structured-extract/1", "experimental": True,
        "qualified_for_production": False, "extraction_status": "success", "warnings": [],
        "image": {"width": width, "height": height, "format": "png"},
        "items": [{"region_bbox": region, "n_rows": n_rows, "n_cols": n_cols, "headers": [], "rows": [],
                   "cells": cells, "has_merged_cells": False, "html": "<table></table>"}],
        "raw_output": {"layout_boxes": [{"cls_id": 8, "label": label, "score": 0.97, "coordinate": box}
                                        for label, box in boxes], "width": width, "height": height},
    }
    if raw is None:
        del data["raw_output"]
    elif raw != "default":
        data["raw_output"] = raw
    return data


def _norm(data):
    return S.normalize_response(data, asset=_Asset(), task="table", filename="t.pdf", response_sha256="b" * 64)


def _decision(data):
    norm = _norm(data)
    return S.publication_refusal(data, task="table", norm=norm), norm


@pytest.fixture(autouse=True)
def _staging(settings):
    settings.DJANGO_ENV = "staging"
    settings.STRUCTURED_VISUAL_ALLOWED_TASKS = []


# =========================================================================== #
# Tableaux valides avec preuve fiable : publiés (NOLAYOUT_01, NOLAYOUT_07)
# =========================================================================== #
class TestValidProof:
    def test_nolayout_01_simple_table_with_table_box_is_published(self):
        refusal, norm = _decision(_response(SIMPLE, 3, 3))
        assert refusal is None and len(norm.tables) == 2
        assert any("Budget (k€):\n125" in t["markdown"] for t in norm.tables)
        meta = norm.units[0].meta["visual"]
        assert meta["requires_visual_check"] is True and meta["source_page"] == 2
        assert meta[S.STRUCTURED_KEY]["experimental"] is True
        assert meta[S.STRUCTURED_KEY]["qualified_for_production"] is False

    def test_nolayout_07_hierarchical_header_with_table_box_keeps_full_paths(self):
        refusal, norm = _decision(_response(HIERARCHICAL, 5, 3))
        assert refusal is None
        lyon = next(t["markdown"] for t in norm.tables if "Site:\nLyon" in t["markdown"])
        assert "Consommation 2025 (MWh) / S1 / Jan:\n12" in lyon
        assert "Consommation 2025 (MWh) / S2 / Juil:\n7" in lyon

    def test_merged_rows_with_table_box_are_published(self):
        refusal, norm = _decision(_response(MERGED, 3, 3))
        assert refusal is None
        assert any("Catégorie:\nÉnergie" in t["markdown"] and "Article:\nGaz" in t["markdown"] for t in norm.tables)

    def test_mixed_image_table_box_beside_a_chart_box_is_published(self):
        data = _response(SIMPLE, 3, 3, region=[38, 37, 536, 279], width=1600, height=1120,
                         boxes=(("chart", [48.05, 389.41, 1323.32, 1043.62]), ("table", [38.98, 37.61, 536.32, 279.9])))
        assert _decision(data)[0] is None

    def test_a_few_pixels_of_rounding_on_the_image_edge_is_tolerated(self):
        data = _response(SIMPLE, 3, 3, region=[0, 0, 1600, 380], boxes=(("table", [0.0, 0.0, 1600.4, 380.3]),))
        assert _decision(data)[0] is None


# =========================================================================== #
# Cellules non vides SANS preuve fiable : refus (NOLAYOUT_02 à 06 + géométrie)
# =========================================================================== #
def _refused(data, reason):
    refusal, norm = _decision(data)
    assert refusal == S.REFUSAL_REGION_UNPROVEN
    assert norm.refusal == (S.REFUSAL_REGION_UNPROVEN, reason)
    assert norm.tables == [] and norm.units == []


class TestMissingOrUnusableProof:
    def test_nolayout_02_layout_absent(self):
        _refused(_response(SIMPLE, 3, 3, raw=None), "layout_missing")

    def test_nolayout_02b_raw_output_without_layout_key(self):
        _refused(_response(SIMPLE, 3, 3, raw={"parsing_res_list": []}), "layout_missing")

    def test_nolayout_03_layout_empty(self):
        _refused(_response(SIMPLE, 3, 3, boxes=()), "layout_empty")

    def test_nolayout_04_no_table_region(self):
        _refused(_response(SIMPLE, 3, 3, boxes=(("text", TABLE_BOX), ("figure_title", [560, 18, 836, 46]))),
                 "no_table_layout_box")

    @pytest.mark.parametrize("box", [
        [38, 39, 748],                       # 3 valeurs
        "38,39,748,339",                     # pas une liste
        [38, 39, "x", 339],                  # non numérique
        [38, 39, math.nan, 339],             # non fini
        [38, 39, math.inf, 339],             # non fini
        [748, 39, 38, 339],                  # inversée
        [38, 39, 38, 339],                   # largeur nulle
        [38, 339, 748, 339],                 # hauteur nulle
        None,
    ])
    def test_nolayout_05_malformed_table_box(self, box):
        _refused(_response(SIMPLE, 3, 3, boxes=(("table", box),)), "no_table_layout_box")

    def test_nolayout_05b_table_box_outside_the_visual(self):
        _refused(_response(SIMPLE, 3, 3, boxes=(("table", [38, 39, 2400, 339]),)), "no_table_layout_box")

    def test_nolayout_05c_other_coordinate_frame_is_never_compared(self):
        """Boîte en coordonnées de page PDF (points, 0-1) : hors repère du visuel -> refus."""
        _refused(_response(SIMPLE, 3, 3, region=[0.02, 0.1, 0.46, 0.89],
                           boxes=(("table", [0.02, 0.1, 0.46, 0.89]),)), "region_invalid")

    def test_nolayout_05d_visual_size_unknown(self):
        data = _response(SIMPLE, 3, 3)
        del data["image"]
        del data["raw_output"]["width"], data["raw_output"]["height"]
        _refused(data, "visual_size_unknown")

    @pytest.mark.parametrize("region", [None, [38, 39, 748], [748, 39, 38, 339], [38, 39, math.nan, 339],
                                        [38, 39, 3000, 339]])
    def test_nolayout_05e_unusable_item_region(self, region):
        _refused(_response(SIMPLE, 3, 3, region=region), "region_invalid")

    def test_nolayout_06_chart_with_non_empty_cells_and_no_table_proof(self):
        chart_box = [38.5, 51.26, 1371.74, 766.49]
        cells = [_cell(0, c, t) for c, t in enumerate(["", "Jan", "Fév"])] + \
                [_cell(1, c, t) for c, t in enumerate(["t", "42", "55"])]
        _refused(_response(cells, 2, 3, region=[38, 51, 1371, 766], width=1400, height=800,
                           boxes=(("chart", chart_box), ("figure_title", [560.6, 18.9, 836.8, 46.8]))),
                 "no_table_layout_box")

    def test_several_regions_none_clearly_holding_the_table(self):
        boxes = (("table", [38, 39, 300, 339]), ("table", [500, 39, 748, 339]))
        _refused(_response(SIMPLE, 3, 3, boxes=boxes), "no_table_layout_box")

    def test_task_table_alone_proves_nothing(self):
        data = _response(SIMPLE, 3, 3, raw=None)
        assert data["task"] == "table" and _decision(data)[0] == S.REFUSAL_REGION_UNPROVEN


# =========================================================================== #
# NOLAYOUT_08 : rien à publier -> statut vide inchangé, pas un refus
# =========================================================================== #
class TestNothingToPublish:
    def test_nolayout_08_no_cells_no_layout_stays_empty(self):
        refusal, norm = _decision(_response([], 0, 0, raw=None))
        assert refusal is None and norm.units == [] and norm.tables == []

    def test_engine_not_found_response_stays_empty(self):
        """Constat (SAFE_CHART_01 en task=table) : ``not_found``, aucun élément."""
        data = _response([], 0, 0)
        data["items"], data["extraction_status"] = [], "not_found"
        refusal, norm = _decision(data)
        assert refusal is None and norm.units == [] and norm.tables == []

    def test_cells_with_only_blank_text_publish_nothing(self):
        refusal, norm = _decision(_response([_cell(0, 0, ""), _cell(0, 1, " ")], 1, 2, raw=None))
        assert refusal is None and norm.tables == [] and norm.units == []


def test_one_unproven_table_blocks_the_whole_response():
    data = _response(SIMPLE, 3, 3)
    second = copy.deepcopy(data["items"][0])
    second["region_bbox"] = [900, 39, 1500, 339]  # aucune boîte table ici
    data["items"].append(second)
    refusal, norm = _decision(data)
    assert refusal == S.REFUSAL_REGION_UNPROVEN and norm.refusal[1] == "no_table_layout_box"
