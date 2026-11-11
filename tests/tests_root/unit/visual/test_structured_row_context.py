"""contexte de ligne, identité du tableau, arbitrage OCR.

Constats PILOT-2 (staging, documents réels Insee) reproduits sur des réponses synthétiques au
format réel ``structured-extract/1`` :
  * C — libellés répétés : deux lignes « Construction » (production / investissement) publiées
    sans leur groupe ; une section pleine largeur (« a) … ») publiée comme une ligne vide ;
  * D — une ligne « Transports et entreposage » d'un tableau de créations d'entreprises a
    répondu à une question sur les prix des transports d'un AUTRE document : rien, dans le
    fragment, ne disait de quel tableau il venait ;
  * B — l'OCR ordinaire de la même page (« Transports et entreposage H 07 62 62 ») a perdu le
    signe que le fragment structuré portait (« -6,2 ») et le générateur l'a suivi.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.visual import structured as S
from apps.rag_core.services.query import prompt_builder as PB

W, H = 1184, 1673


class _Asset:
    content_hash = "a" * 64
    ordinal = 0
    source_page = 2
    source_locator = None


def _cell(r, c, text, rs=1, cs=1):
    return {"row": r, "col": c, "row_span": rs, "col_span": cs, "text": text}


def _response(rows, *, region=(214, 98, 967, 491), extra_cells=(), titles=(), n_rows=None):
    cells = [_cell(r, c, t) for r, row in enumerate(rows) for c, t in enumerate(row) if t is not None]
    cells += list(extra_cells)
    n_cols = max(len(r) for r in rows)
    boxes = [{"label": "table", "score": 0.97, "coordinate": [region[0] - 1, region[1] - 1, region[2] + 1, region[3] + 1]}]
    ocr = {"rec_texts": [], "rec_scores": [], "rec_boxes": []}
    for label, text, box in titles:
        boxes.append({"label": label, "score": 0.9, "coordinate": list(box)})
        ocr["rec_texts"].append(text)
        ocr["rec_scores"].append(0.98)
        ocr["rec_boxes"].append(list(box))
    return {"task": "table", "contract": "structured-extract/1", "experimental": True,
            "qualified_for_production": False, "extraction_status": "success", "warnings": [],
            "image": {"width": W, "height": H},
            "items": [{"region_bbox": list(region), "n_rows": n_rows or len(rows), "n_cols": n_cols,
                       "headers": [], "rows": [], "cells": cells, "has_merged_cells": True, "html": ""}],
            "raw_output": {"layout_boxes": boxes, "ocr": ocr, "width": W, "height": H}}


def _norm(data):
    return S.normalize_response(data, asset=_Asset(), task="table", filename="note.pdf", response_sha256="c" * 64)


def _rows(norm):
    return [t["markdown"] for t in norm.tables]


@pytest.fixture(autouse=True)
def _staging(settings):
    settings.DJANGO_ENV = "staging"
    settings.STRUCTURED_VISUAL_ALLOWED_TASKS = []


HEAD = ["", "2025T1", "2025T2", "2025T3"]


# =========================================================================== #
# C — sections pleine largeur et libellés répétés
# =========================================================================== #
class TestRowGroups:
    def test_full_width_section_rows_become_the_row_group(self):
        rows = [HEAD, [None] * 4, ["Ensemble hors tabac", "9 836", "119,67", "0,9"],
                [None] * 4, ["Ensemble hors tabac", "9 731", "119,27", "0,6"]]
        data = _response(rows, extra_cells=[_cell(1, 0, "a) Ensemble des ménages", cs=4),
                                            _cell(3, 0, "c) Ménages du 1er quintile", cs=4)])
        norm = _norm(data)
        assert S.publication_refusal(data, task="table", norm=norm) is None
        texts = _rows(norm)
        assert len(texts) == 2, "une section n'est pas une ligne de données"
        c = next(t for t in texts if "0,6" in t)
        assert "Groupe : c) Ménages du 1er quintile" in c and "Groupe : a)" not in c
        a = next(t for t in texts if "0,9" in t)
        assert "Groupe : a) Ensemble des ménages" in a
        metas = {t["meta"]["row_group_path"][0] for t in norm.tables}
        assert metas == {"a) Ensemble des ménages", "c) Ménages du 1er quintile"}

    def test_repeated_label_without_structural_group_is_signalled_not_guessed(self):
        """Constat IR300 : groupes en gras, AUCUN signal de grille -> pas de groupe inventé."""
        rows = [HEAD, ["Production branche", "0,1", "0,3", "0,8"], ["Construction", "-0,7", "-0,1", "0,2"],
                ["FBCF", "-0,1", "0,0", "0,5"], ["Construction", "-1,1", "-0,3", "0,1"]]
        norm = _norm(_response(rows))
        constructions = [t for t in norm.tables if "Construction" in t["markdown"]]
        assert len(constructions) == 2
        for i, t in enumerate(sorted(constructions, key=lambda t: t["meta"]["logical_row_id"]), start=1):
            assert t["meta"]["row_group_ambiguous"] is True and t["meta"]["row_group_path"] == []
            assert f"Libellé « Construction » répété dans ce tableau (occurrence {i}/2)" in t["markdown"]
            assert "groupe parent non déterminé" in t["markdown"]
        prod = next(t for t in norm.tables if "Production branche" in t["markdown"])
        assert prod["meta"]["row_group_ambiguous"] is False

    def test_lost_section_label_before_repeated_rows_is_refused(self):
        """Constat IPC : le moteur rend une section pleine largeur VIDE (« b) » perdu) ; les lignes
        homonymes qui suivent ne sont plus distinguables -> refus, jamais un groupe deviné."""
        rows = [HEAD, [None] * 4, ["Ensemble hors tabac", "9 836", "119,67", "0,9"], [None] * 4,
                ["Ensemble hors tabac", "9 748", "118,94", "0,9"]]
        data = _response(rows, extra_cells=[_cell(1, 0, "a) Ensemble des ménages", cs=4), _cell(3, 0, "", cs=4)])
        assert S.publication_refusal(data, task="table", norm=_norm(data)) == S.REFUSAL_ROW_GROUP_AMBIGUOUS

    def test_lost_section_label_without_repeated_rows_is_published(self):
        rows = [HEAD, [None] * 4, ["Alimentation", "1 464", "133,51", "1,4"], [None] * 4, ["Énergie", "807", "150,23", "-4,6"]]
        data = _response(rows, extra_cells=[_cell(1, 0, "a) Section", cs=4), _cell(3, 0, "", cs=4)])
        norm = _norm(data)
        assert S.publication_refusal(data, task="table", norm=norm) is None
        energie = next(t for t in norm.tables if "Énergie" in t["markdown"])
        assert energie["meta"]["row_group_ambiguous"] is True and "Groupe :" not in energie["markdown"]

    def test_simple_table_is_unchanged(self):
        rows = [["Poste", "Budget", "Réalisé"], ["Énergie", "125", "118"], ["Eau", "34", "31"]]
        norm = _norm(_response(rows))
        assert all(t["meta"]["row_group_ambiguous"] is False and t["meta"]["row_group_path"] == [] for t in norm.tables)
        assert all("Context:" not in t["markdown"] for t in norm.tables)


# =========================================================================== #
# D — identité du tableau portée par chaque ligne
# =========================================================================== #
class TestTableIdentity:
    def test_title_box_right_above_the_table_names_every_row(self):
        rows = [["Nomenclature agrégée", "Code", "sur 3 mois"], ["Transports et entreposage", "H", "-6,2"]]
        data = _response(rows, titles=[("paragraph_title", "Évolution des créations d'entreprises par secteur", (214, 60, 900, 88))])
        norm = _norm(data)
        row = _rows(norm)[0]
        assert "Tableau : Évolution des créations d'entreprises par secteur" in row
        assert norm.tables[0]["meta"]["table_title"] == "Évolution des créations d'entreprises par secteur"

    def test_title_far_away_or_below_is_not_used(self):
        rows = [["Poste", "Budget"], ["Énergie", "125"]]
        data = _response(rows, titles=[("paragraph_title", "Révisions", (214, 520, 600, 548)),
                                       ("paragraph_title", "Pour en savoir plus", (214, 10, 600, 30))])
        norm = _norm(data)
        assert "Tableau :" not in _rows(norm)[0] and norm.tables[0]["meta"]["table_title"] is None


# =========================================================================== #
# B — arbitrage entre fragment structuré et OCR ordinaire de la même page
# =========================================================================== #
STRUCT_ROW = ("Context: Tableau : Évolution des créations d'entreprises par secteur\n\n"
              "Nomenclature agrégée:\nTransports et entreposage\n\nCode:\nH\n\n"
              "Variation mensuelle / Novembre 25 / Octobre 25:\n0,7\n\n"
              "Glissement annuel / sur 3 mois:\n-6,2\n\nGlissement annuel / sur 12 mois:\n6,2")
OCR_PAGE = ("Évolution des créations d'entreprises par secteur d'activité\n"
            "Transports et entreposage H 07 62 62\n"
            "Information et communication JZ 4,4 21,1 4,5\n"
            "Les créations progressent dans les services.")


def _src(cid, doc, page, text, *, structured=False):
    meta = {"kind": "table", "visual": {"task": "table", "structured": {}}} if structured else {}
    return {"chunk_id": cid, "text": text, "source": {"document_id": doc, "page_number": page, "meta": meta}}


def _prepared(sources):
    return {c["chunk_id"]: c["text"] for c in PB._prepare_selected_for_prompt(sources, snippet_max_chars=4000)}


class TestSamePageArbitration:
    def test_sign_lost_by_ocr_is_masked_when_the_structured_row_is_in_context(self):
        out = _prepared([_src("s", "D1", 4, STRUCT_ROW, structured=True), _src("o", "D1", 4, OCR_PAGE)])
        assert "Transports et entreposage H 07 62 62" not in out["o"]
        assert PB.ARBITRATED_LINE_MARKER in out["o"]
        assert "Information et communication JZ 4,4 21,1 4,5" in out["o"], "les autres lignes restent"
        assert "Les créations progressent" in out["o"]
        assert "-6,2" in out["s"]

    def test_decimal_separator_lost_by_ocr_is_masked(self):
        struct = "Poste:\nEnsemble hors tabac\n\nPondérations:\n9 731\n\nIndices:\n119,27\n\nMois:\n-0,2\n\nAn:\n0,6"
        ocr = "Ensemble hors tabac 9731 119,27 -02 06"
        out = _prepared([_src("s", "D1", 3, struct, structured=True), _src("o", "D1", 3, ocr)])
        assert "-02 06" not in out["o"] and PB.ARBITRATED_LINE_MARKER in out["o"]

    def test_other_document_is_never_touched(self):
        out = _prepared([_src("s", "D1", 4, STRUCT_ROW, structured=True), _src("o", "D2", 4, OCR_PAGE)])
        assert out["o"] == OCR_PAGE

    def test_other_page_is_never_touched(self):
        out = _prepared([_src("s", "D1", 4, STRUCT_ROW, structured=True), _src("o", "D1", 3, OCR_PAGE)])
        assert out["o"] == OCR_PAGE

    def test_same_label_other_values_is_not_the_same_evidence(self):
        ocr = "Transports et entreposage H 1,1 3,4 5,0"
        out = _prepared([_src("s", "D1", 4, STRUCT_ROW, structured=True), _src("o", "D1", 4, ocr)])
        assert out["o"] == ocr

    def test_agreeing_ocr_line_is_kept(self):
        ocr = "Transports et entreposage H 0,7 -6,2 6,2"
        out = _prepared([_src("s", "D1", 4, STRUCT_ROW, structured=True), _src("o", "D1", 4, ocr)])
        assert out["o"] == ocr

    def test_overview_grid_rows_also_arbitrate(self):
        overview = "Tableau (extraction structurée expérimentale)\nNomenclature agrégée | Code | M | 3 mois | 12 mois\nTransports et entreposage | H | 0,7 | -6,2 | 6,2"
        out = _prepared([_src("s", "D1", 4, overview, structured=True), _src("o", "D1", 4, OCR_PAGE)])
        assert "H 07 62 62" not in out["o"]

    def test_without_structured_evidence_ordinary_text_is_untouched(self):
        out = _prepared([_src("o", "D1", 4, OCR_PAGE)])
        assert out["o"] == OCR_PAGE
