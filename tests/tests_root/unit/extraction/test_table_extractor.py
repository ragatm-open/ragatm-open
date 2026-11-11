"""Tests P3-a1 — extraction tabulaire structurée (module pur, sans DB).

La logique de construction de tableau est testée via `build_table_from_rows`
(lignes brutes synthétiques = sortie pdfplumber) → aucun PDF/pdfplumber requis.
Un test d'intégration réel est gardé par `importorskip` (skip si pdfplumber absent).
"""
import pytest

from apps.rag_core.services.ingest.extraction.table_extractor import (
    ExtractedTable,
    build_table_from_rows,
    detect_table_type,
    extract_tables_from_pdf_bytes,
    normalize_headers,
    normalize_rebar_diameter,
    parse_number_fr,
    table_to_markdown,
    table_to_meta,
)


# ----------------------------------------------------- helpers purs
class TestParseNumberFr:
    @pytest.mark.parametrize("raw,expected", [
        ("369,92", 369.92),
        ("1 208,5", 1208.5),
        ("-12,30", -12.3),
        ("+4.5", 4.5),
        ("0", 0.0),
        ("-160", -160.0),
        ("1 208,5", 1208.5),   # espace insécable (milliers)
    ])
    def test_ok(self, raw, expected):
        assert parse_number_fr(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "", "   ", None, "ELU+", "-", "12.0.0"])
    def test_none(self, raw):
        assert parse_number_fr(raw) is None


class TestRebarDiameter:
    def test_ok(self):
        assert normalize_rebar_diameter("HA 14") == "HA14"
        assert normalize_rebar_diameter("ha12") == "HA12"
        assert normalize_rebar_diameter("HA8") == "HA8"

    def test_grade_is_not_diameter(self):
        assert normalize_rebar_diameter("HA 500") is None
        assert normalize_rebar_diameter("") is None
        assert normalize_rebar_diameter(None) is None


class TestHeaders:
    def test_detect_pile_head_forces_separate_cols(self):
        h = ["Noeud", "Cas", "FX", "FY", "FZ", "MX", "MY", "MZ"]
        assert detect_table_type(h) == "pile_head_forces"

    def test_detect_pile_head_forces_combined_and_units(self):
        h = ["Noeud/Cas", "FX [T]", "FY [T]", "FZ [T]", "MX [Tm]", "MY [Tm]", "MZ [Tm]"]
        assert detect_table_type(h) == "pile_head_forces"
        assert normalize_headers(h)[0] == "node_case"

    def test_unknown_or_ambiguous_is_none(self):
        assert detect_table_type(["Repère", "Ø", "Nombre", "Longueur"]) is None
        assert detect_table_type(["Noeud"]) is None  # pas de FZ


# ----------------------------------------------------- construction de table
def _forces_rows():
    return [
        ["Noeud/Cas", "FX [T]", "FY [T]", "FZ [T]", "MX [Tm]", "MY [Tm]", "MZ [Tm]"],
        ["153/ELU+", "-11,4", "-3,1", "-160", "0", "0", "0"],
        ["150/ELU-", "-0,2", "15,5", "54,8", "", "0", "0"],   # MX vide -> None
    ]


class TestBuildTable:
    def test_pile_head_forces_structured(self):
        t = build_table_from_rows(_forces_rows(), page_number=18)
        assert isinstance(t, ExtractedTable)
        assert t.table_type == "pile_head_forces"
        assert t.page_number == 18
        assert t.units == {"force": "T", "moment": "Tm"}
        assert t.confidence == "high"
        assert t.requires_visual_check is False

        r0 = t.rows[0]
        assert r0["node"] == "153" and r0["case"] == "ELU+"
        assert r0["fx"] == -11.4 and r0["fz"] == -160.0 and r0["mz"] == 0.0
        assert "153/ELU+" in r0["source_text"]

    def test_empty_cell_is_null(self):
        t = build_table_from_rows(_forces_rows(), page_number=1)
        assert t.rows[1]["mx"] is None       # cellule vide -> None (jamais inventée)
        assert t.rows[1]["fz"] == 54.8

    def test_blank_row_ignored(self):
        rows = _forces_rows() + [["", "", "", "", "", "", ""]]
        t = build_table_from_rows(rows, page_number=1)
        assert len(t.rows) == 2  # la ligne vide n'est pas inventée

    def test_unknown_table_ignored(self):
        rows = [["Repère", "Ø", "Nombre"], ["S1", "HA12", "8"]]
        assert build_table_from_rows(rows, page_number=1) is None

    def test_too_few_rows(self):
        assert build_table_from_rows([["Noeud/Cas", "FZ [T]"]], page_number=1) is None

    def test_partial_headers_medium_confidence(self):
        rows = [["Noeud", "FX [T]", "FZ [T]"], ["153/ELU", "1,2", "-3,4"]]
        t = build_table_from_rows(rows, page_number=2)
        assert t is not None
        assert t.confidence == "medium"
        assert t.requires_visual_check is True


# ----------------------------------------------------- rendu
class TestRender:
    def test_markdown_contains_title_page_values(self):
        t = build_table_from_rows(_forces_rows(), page_number=18)
        md = table_to_markdown(t)
        assert "Efforts en têtes de pieux" in md
        assert "Page : 18" in md
        assert "forces = T" in md and "moments = Tm" in md
        assert "-160" in md and "153" in md
        assert "| node | case | fx |" in md

    def test_markdown_visual_check_mention(self):
        rows = [["Noeud", "FX [T]", "FZ [T]"], ["153/ELU", "1,2", "-3,4"]]
        md = table_to_markdown(build_table_from_rows(rows, page_number=2))
        assert "à vérifier" in md.lower()

    def test_meta_roundtrip(self):
        t = build_table_from_rows(_forces_rows(), page_number=18)
        meta = table_to_meta(t)
        assert meta["table_type"] == "pile_head_forces"
        assert meta["columns"] == ["node", "case", "fx", "fy", "fz", "mx", "my", "mz"]
        assert meta["rows"][0]["fz"] == -160.0
        assert t.as_dict() == meta


# ----------------------------------------------------- pdfplumber (contrôlé)
class TestPdfPlumber:
    def test_empty_bytes(self):
        assert extract_tables_from_pdf_bytes(b"") == []

    def test_absent_or_invalid_is_controlled(self):
        # pdfplumber absent -> [] ; présent mais bytes non-PDF -> [] (pas de crash).
        assert extract_tables_from_pdf_bytes(b"not a pdf at all") == []

    def test_real_pdf_integration(self):
        pdfplumber = pytest.importorskip("pdfplumber")  # noqa: F841
        reportlab = pytest.importorskip("reportlab")  # noqa: F841
        from io import BytesIO

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

        buf = BytesIO()
        data = [
            ["Noeud/Cas", "FX [T]", "FY [T]", "FZ [T]", "MX [Tm]", "MY [Tm]", "MZ [Tm]"],
            ["153/ELU+", "-11.4", "-3.1", "-160", "0", "0", "0"],
        ]
        table = Table(data)
        # Grille visible : pdfplumber détecte les tableaux via les lignes (stratégie défaut).
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        SimpleDocTemplate(buf, pagesize=A4).build([table])
        tables = extract_tables_from_pdf_bytes(buf.getvalue())
        assert tables, "pdfplumber devrait détecter la table"
        t = tables[0]
        assert t.table_type == "pile_head_forces"
        assert any(r["node"] == "153" and r["fz"] == -160.0 for r in t.rows)


# ----------------------------------------------------- P3-b : reconstruction OCR
from apps.rag_core.services.ingest.extraction.table_extractor import (  # noqa: E402
    reconstruct_rows_from_ocr_words,
    extract_ocr_tables_from_pages,
)


def _ocr_words():
    """Mots OCR synthétiques d'un pile_head_forces (header + 1 ligne)."""
    header = [("Noeud/Cas", 10), ("FX", 110), ("[T]", 140), ("FZ", 210), ("MZ", 360)]
    # NB : on garde un sous-ensemble de colonnes (node_case + FX + FZ + MZ) -> medium côté
    # build, mais P3-b force low de toute façon. FZ présent => détecté pile_head_forces.
    row1 = [("153/ELU+", 10), ("-11,4", 110), ("-160", 210), ("0", 360)]
    words = []
    for ln, cells in enumerate((header, row1), start=1):
        for (txt, left) in cells:
            words.append({"text": txt, "left": left, "top": ln * 20,
                          "block_num": 1, "par_num": 1, "line_num": ln})
    return words


class TestOcrReconstruction:
    def test_reconstruct_rows_groups_and_merges_units(self):
        rows = reconstruct_rows_from_ocr_words(_ocr_words())
        assert rows[0][0] == "Noeud/Cas"
        assert "FX [T]" in rows[0]          # unité fusionnée à l'en-tête
        assert rows[1][0] == "153/ELU+"     # ligne de données dans l'ordre

    def test_extract_ocr_tables_forces_low_confidence(self):
        tables = extract_ocr_tables_from_pages({18: _ocr_words()})
        assert len(tables) == 1
        t = tables[0]
        assert t.table_type == "pile_head_forces"
        assert t.source == "ocr"
        assert t.confidence == "low"                 # OCR => dégradé
        assert t.requires_visual_check is True
        r = t.rows[0]
        assert r["node"] == "153" and r["case"] == "ELU+" and r["fz"] == -160.0
        assert r["requires_visual_check"] is True

    def test_empty_words_no_table(self):
        assert reconstruct_rows_from_ocr_words([]) == []
        assert extract_ocr_tables_from_pages({1: []}) == []

    def test_non_forces_words_ignored(self):
        words = [{"text": t, "left": l, "top": 10, "line_num": 1, "block_num": 1, "par_num": 1}
                 for t, l in [("Repère", 10), ("Nombre", 100)]]
        assert extract_ocr_tables_from_pages({1: words}) == []
