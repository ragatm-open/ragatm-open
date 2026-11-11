"""Lot E2 — extracteur Excel `.xlsx` : extraction, valeurs, formules, blocs, chunking.

Les décisions du spike E1 sont ici VERROUILLÉES par des tests : si quelqu'un retire
`ws.reset_dimensions()`, ou fait renvoyer `0` à une formule sans cache, un test tombe.
"""
import pytest

from apps.rag_core.services.ingest.extraction import (
    CorruptedFile,
    ExtractionLimitExceeded,
    RejectedFile,
    extract_document,
)
from apps.rag_core.services.ingest.extraction import excel_extractor as XE
from apps.rag_core.services.ingest.extraction import mime as M

from ._builders import (
    build_xlsx,
    make_xlsx_lying_dimension,
    make_xlsx_with_cached_formulas,
    raw_sheet,
    make_raw_xlsx,
)


def _x(data, name="classeur.xlsx"):
    return extract_document(data, filename=name)


def _chunks(r):
    return r.excel_chunks


def _all_text(r):
    return "\n".join(c["text"] for c in r.excel_chunks)


# --------------------------------------------------------------------------- #
# Nominal
# --------------------------------------------------------------------------- #
class TestNominal:
    def test_simple_workbook(self):
        r = _x(build_xlsx({"Budget": [
            ["Projet", "Budget", "Realise"],
            ["Atlas", 250000, 220000],
            ["Orion", 180000, 195000],
        ]}))
        assert r.status.value == "success"
        assert r.detected_mime == M.XLSX
        assert r.method == "excel:openpyxl"
        assert r.pages is None                 # une feuille n'est PAS une page
        assert r.blocks
        assert len(_chunks(r)) == 1

        c = _chunks(r)[0]
        assert "| Projet | Budget | Realise |" in c["text"]
        assert "| Atlas | 250000 | 220000 |" in c["text"]
        assert c["meta"]["sheet_name"] == "Budget"
        assert c["meta"]["cell_range"] == "A2:C3"      # lignes de DONNÉES
        assert c["meta"]["header_row"] == 1
        assert c["meta"]["headers"] == ["Projet", "Budget", "Realise"]
        assert c["meta"]["headers_inferred"] is False
        assert c["meta"]["requires_visual_check"] is False
        assert c["meta"]["kind"] == "excel_table"

    def test_context_line_is_indexed_not_only_meta(self):
        """Le nom de la feuille doit être RETROUVABLE : aucune cellule ne le contient."""
        r = _x(build_xlsx({"Risques": [["Item", "Niveau"], ["Incendie", 3]]}))
        assert "Feuille : Risques" in _all_text(r)

    def test_multi_sheets(self):
        r = _x(build_xlsx({
            "A": [["x", "y"], [1, 2]],
            "B": [["u", "v"], [3, 4]],
        }))
        names = {c["meta"]["sheet_name"] for c in _chunks(r)}
        assert names == {"A", "B"}
        assert r.metadata["excel"]["sheets_indexed"] == 2

    def test_metadata_carries_versions(self):
        r = _x(build_xlsx({"S": [["a"], [1]]}))
        assert r.metadata["extractor"] == "excel"
        assert r.metadata["extractor_version"] == XE.EXTRACTOR_VERSION
        assert r.metadata["chunking_version"] == XE.CHUNKING_VERSION

    def test_empty_workbook_is_empty_not_success(self):
        r = _x(build_xlsx({"Vide": [[None, None]]}))
        assert r.status.value == "empty"
        assert not _chunks(r)


# --------------------------------------------------------------------------- #
# 🔴 Dimension déclarée — la réserve la plus grave du spike E1
# --------------------------------------------------------------------------- #
class TestDeclaredDimension:
    def test_lying_small_dimension_loses_nothing(self):
        """Dimension déclarée `A1:A1`, contenu réel `A1:E10` = 50 cellules.

        Sans `ws.reset_dimensions()`, openpyxl n'en rend QU'UNE : 49 cellules
        disparaîtraient sans erreur, sans warning, sans exception (mesuré en E1).
        """
        r = _x(make_xlsx_lying_dimension())
        assert r.status.value == "success"
        assert r.metadata["excel"]["cells_read"] == 50
        assert "excel_dimension_reset" in r.warnings
        # La dernière valeur (E10 = 10 × 5 = 50) doit être présente.
        assert "50" in _all_text(r)

    def test_reset_dimensions_is_actually_called(self, monkeypatch):
        """Preuve directe : si `reset_dimensions()` disparaît, ce test tombe."""
        # L'extraction s'exécute dans un processus enfant (HARDENING-03) : une liste
        # en mémoire y resterait confinée.
        from tests_root.process_witness import TemoinPartage

        calls = TemoinPartage()
        from openpyxl.worksheet._read_only import ReadOnlyWorksheet

        original = ReadOnlyWorksheet.reset_dimensions

        def _spy(self):
            calls.note(self.title)
            return original(self)

        monkeypatch.setattr(ReadOnlyWorksheet, "reset_dimensions", _spy)
        _x(build_xlsx({"S1": [["a"], [1]], "S2": [["b"], [2]]}))
        assert calls.valeurs == ["S1", "S2"]

    def test_honest_dimension_raises_no_warning(self):
        """Pas de bruit : un classeur sain ne doit PAS porter excel_dimension_reset."""
        r = _x(build_xlsx({"S": [["a", "b"], [1, 2]]}))
        assert "excel_dimension_reset" not in r.warnings


# --------------------------------------------------------------------------- #
# 🔴 Formules — cache présent / absent
# --------------------------------------------------------------------------- #
class TestFormulas:
    def test_cached_value_is_indexed(self):
        r = _x(make_xlsx_with_cached_formulas())
        assert "30000" in _all_text(r)          # =B2-C2, cache = 30000

    def test_uncached_formula_is_empty_never_zero(self):
        """LE piège : `data_only=True` renvoie None — comme une cellule vide.

        Sans le scan XML, on indexerait une case vide sans le dire. Et surtout : on ne
        doit JAMAIS écrire `0` (un moteur de calcul donnerait -15000 pour =B3-C3).
        """
        r = _x(make_xlsx_with_cached_formulas())
        ex = r.metadata["excel"]

        assert ex["formula_count"] == 2
        assert ex["formula_cached_count"] == 1
        assert ex["formula_uncached_count"] == 1
        assert ex["requires_recalculation"] is True
        assert ex["uncached_formula_cells"] == ["Feuil1!D3"]
        assert "excel_formulas_not_cached" in r.warnings

        # La ligne Orion existe, mais sa colonne Ecart est VIDE — jamais 0, jamais -15000.
        orion = [ln for ln in _all_text(r).splitlines() if "Orion" in ln][0]
        assert orion.endswith("|  |") or orion.rstrip().endswith("| |")
        assert "-15000" not in orion
        assert "| 0 |" not in orion

    def test_formula_text_is_never_indexed(self):
        r = _x(make_xlsx_with_cached_formulas())
        text = _all_text(r)
        assert "B2-C2" not in text
        assert "=" not in text.replace("Feuille :", "")

    def test_excel_error_is_preserved(self):
        """Une erreur Excel est la VÉRITÉ du document : ni None, ni 0."""
        r = _x(make_xlsx_with_cached_formulas())
        assert "#DIV/0!" in _all_text(r)
        assert r.metadata["excel"]["error_cells"] == 1
        assert "excel_error_cells" in r.warnings


# --------------------------------------------------------------------------- #
# Normalisation des valeurs (bout en bout)
# --------------------------------------------------------------------------- #
class TestValues:
    def test_types_render_faithfully(self):
        from datetime import datetime

        r = _x(build_xlsx({"T": [
            ["Type", "Valeur"],
            ["entier", 42],
            ["decimal", 1234.56],
            ["date", (datetime(2026, 7, 13), "yyyy-mm-dd")],
            ["datetime", (datetime(2026, 7, 13, 14, 30), "yyyy-mm-dd hh:mm")],
            ["pourcent", (0.1, "0%")],
            ["pourcent2", (0.085, "0.00%")],
            ["devise", (1234.56, '#,##0.00\\ "€"')],
            ["bool_vrai", True],
            ["bool_faux", False],
            ["texte10pct", "10%"],
            ["zeros", "00123"],
            ["nb_texte", "1 234,56"],
        ]}))
        t = _all_text(r)

        assert "| entier | 42 |" in t
        assert "| decimal | 1234.56 |" in t               # PAS de locale implicite
        assert "| date | 2026-07-13 |" in t               # ISO
        assert "| datetime | 2026-07-13T14:30:00 |" in t
        assert "| pourcent | 10% |" in t                  # number_format APPLIQUÉ
        assert "| pourcent2 | 8.50% |" in t
        assert "| devise | 1234.56 |" in t                # nombre BRUT, € en méta
        assert "| bool_vrai | TRUE |" in t
        assert "| bool_faux | FALSE |" in t
        assert "| texte10pct | 10% |" in t                # texte, pas coercé
        assert "| zeros | 00123 |" in t                   # zéros de tête préservés
        assert "| nb_texte | 1 234,56 |" in t             # aucune coercition
        assert "€" in r.metadata["excel"]["currencies"]

    def test_big_integer_keeps_full_precision(self):
        """Entier > 2^53 : aucune perte de précision à la LECTURE.

        Fixture en XML brut : c'est le chemin réel (un vrai .xlsx vient d'Excel).
        openpyxl, lui, perd la précision à l'ÉCRITURE — mais la production ne fait
        que lire, jamais écrire.
        """
        body = (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>grand</t></is></c>'
            '<c r="B1"><v>9007199254740993</v></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>autre</t></is></c>'
            '<c r="B2"><v>1</v></c></row>'
        )
        r = _x(make_raw_xlsx(raw_sheet(body, dimension="A1:B2")))
        assert "9007199254740993" in _all_text(r)

    def test_true_duration_over_24h(self):
        """`[h]:mm:ss` -> openpyxl rend un timedelta -> `60:00:00`. Fidèle."""
        r = _x(build_xlsx({"D": [["duree"], [(2.5, "[h]:mm:ss")]]}))
        assert "60:00:00" in _all_text(r)

    def test_time_format_over_24h_never_becomes_a_fake_date(self):
        """Le piège E1 : `h:mm:ss` sur 2,5 jours -> openpyxl rend datetime(1900,1,2,12:00).

        Indexer `1900-01-02T12:00:00` serait une FAUSSE DATE. Excel n'affiche que
        l'heure pour ce format : on rend l'heure, et on le signale.
        """
        r = _x(build_xlsx({"D": [["duree"], [(2.5, "h:mm:ss")]]}))
        t = _all_text(r)
        assert "1900" not in t
        assert "12:00:00" in t
        assert "excel_duration_format_unsupported" in r.warnings

    def test_long_cell_is_truncated_not_rejected(self, settings):
        settings.DOCUMENT_EXCEL_MAX_CELL_CHARS = 50
        r = _x(build_xlsx({"L": [["texte"], ["x" * 500]]}))
        assert r.status.value == "success"          # troncature, PAS de rejet
        assert "excel_cell_truncated" in r.warnings
        assert r.metadata["excel"]["truncated_cells"] == 1

    def test_formula_injection_in_text_cell_is_neutralized(self):
        """Une cellule TEXTE dont le contenu commence par `=` (DDE, injection).

        Fixture en XML brut : openpyxl transformerait `"=cmd|…"` en FORMULE, alors
        qu'on veut précisément le cas où c'est du **texte** qui ressemble à une formule.
        """
        body = (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>cmd</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>=cmd|\'/c calc\'!A0</t></is></c></row>'
        )
        r = _x(make_raw_xlsx(raw_sheet(body, dimension="A1:A2")))
        assert "excel_formula_injection_neutralized" in r.warnings
        assert "'=cmd" in _all_text(r)


# --------------------------------------------------------------------------- #
# Feuilles
# --------------------------------------------------------------------------- #
class TestSheets:
    def test_hidden_is_indexed_but_very_hidden_is_not(self):
        """Politique `hidden_only` (défaut depuis la campagne AirWorks).

        `hidden` = commodité de présentation, réaffichable d'un clic droit : indexée,
        et SIGNALÉE (`sheet_state`). `veryHidden` = non modifiable depuis l'interface
        d'Excel, donc intention délibérée : exclue.

        Avant, les deux étaient sautées — et un registre financier entier (la feuille
        « Convertibles » d'un vrai classeur) restait introuvable.
        """
        r = _x(build_xlsx(
            {
                "Visible": [["a"], [1]],
                "Cachee": [["registre convertible"], [999]],
                "TresCachee": [["scratch"], [888]],
            },
            hidden=("Cachee",),
            very_hidden=("TresCachee",),
        ))
        t = _all_text(r)
        assert "registre convertible" in t          # indexée…
        assert "scratch" not in t                   # …mais pas veryHidden

        cachee = next(c for c in r.excel_chunks if c["meta"]["sheet_name"] == "Cachee")
        assert cachee["meta"]["sheet_state"] == "hidden"   # …et jamais en silence
        assert "excel_veryhidden_sheet_skipped" in r.warnings

        ex = r.metadata["excel"]
        assert ex["sheets_total"] == 3
        assert ex["sheets_indexed"] == 2              # Visible + Cachee
        assert ex["hidden_sheets_indexed"] == 1
        assert ex["hidden_sheets_indexed_names"] == ["Cachee"]
        assert ex["veryhidden_sheets_skipped"] == 1

    def test_hidden_sheets_can_be_included_by_config(self, settings):
        settings.DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS = True
        r = _x(build_xlsx({"V": [["a"], [1]], "H": [["visible maintenant"], [2]]},
                          hidden=("H",)))
        assert "visible maintenant" in _all_text(r)
        hidden_chunk = [c for c in _chunks(r) if c["meta"]["sheet_name"] == "H"][0]
        assert hidden_chunk["meta"]["sheet_hidden"] is True

    def test_empty_sheet_is_counted_and_skipped(self):
        r = _x(build_xlsx({"Plein": [["a"], [1]], "Vide": [[None]]}))
        assert r.metadata["excel"]["sheets_empty"] == 1
        assert "excel_empty_sheets_skipped" in r.warnings

    def test_too_many_sheets_rejected(self, settings):
        settings.DOCUMENT_EXCEL_MAX_SHEETS = 2
        with pytest.raises(ExtractionLimitExceeded) as ei:
            _x(build_xlsx({f"S{i}": [["a"], [i]] for i in range(5)}))
        assert ei.value.code == "excel_too_many_sheets"


# --------------------------------------------------------------------------- #
# Blocs
# --------------------------------------------------------------------------- #
class TestBlocks:
    def test_two_tables_separated_by_an_empty_row(self):
        r = _x(build_xlsx({"S": [
            ["Projet", "Budget"],
            ["Atlas", 100],
            [],
            ["Risque", "Niveau"],
            ["Incendie", 3],
        ]}))
        assert len(_chunks(r)) == 2
        assert _chunks(r)[0]["meta"]["headers"] == ["Projet", "Budget"]
        assert _chunks(r)[1]["meta"]["headers"] == ["Risque", "Niveau"]
        assert _chunks(r)[1]["meta"]["table_index"] == 2

    def test_title_above_a_table(self):
        r = _x(build_xlsx({"S": [
            ["Synthese budgetaire"],
            [],
            ["Projet", "Budget"],
            ["Atlas", 100],
        ]}))
        c = _chunks(r)[0]
        assert c["meta"]["title"] == "Synthese budgetaire"
        assert "Synthese budgetaire" in c["text"]        # répété dans le chunk

    def test_headers_inferred_when_first_row_is_data(self):
        r = _x(build_xlsx({"S": [[1, 2, 3], [4, 5, 6]]}))
        c = _chunks(r)[0]
        assert c["meta"]["headers_inferred"] is True
        assert c["meta"]["headers"] == ["col1", "col2", "col3"]
        assert c["meta"]["header_row"] is None
        assert "| 1 | 2 | 3 |" in c["text"]              # aucune ligne perdue

    def test_unnamed_column_gets_a_placeholder(self):
        r = _x(build_xlsx({"S": [["Projet", None, "Budget"], ["Atlas", "x", 100]]}))
        assert _chunks(r)[0]["meta"]["headers"] == ["Projet", "col2", "Budget"]

    def test_narrative_sheet_is_not_a_table(self):
        r = _x(build_xlsx({"Notes": [
            ["Le projet Atlas avance conformement au calendrier."],
            ["Un risque de retard subsiste sur le lot 3."],
            ["La revue est prevue en septembre."],
        ]}))
        c = _chunks(r)[0]
        assert c["meta"]["kind"] == "excel_narrative"
        assert "|" not in c["text"].split("\n\n", 1)[1]   # pas de tableau Markdown
        assert "Atlas avance" in c["text"]

    def test_interior_empty_column_is_kept(self):
        """Supprimer une colonne vide INTÉRIEURE rendrait la plage citée fausse."""
        r = _x(build_xlsx({"S": [["A", None, "C"], [1, None, 3]]}))
        c = _chunks(r)[0]
        assert c["meta"]["column_start"] == "A"
        assert c["meta"]["column_end"] == "C"
        assert len(c["meta"]["headers"]) == 3


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
class TestChunking:
    def test_row_window_and_no_overlap(self, settings):
        settings.DOCUMENT_EXCEL_ROWS_PER_CHUNK = 10
        settings.DOCUMENT_EXCEL_CHUNK_MAX_CHARS = 100_000
        rows = [["Projet", "Budget"]] + [[f"P{i}", i] for i in range(1, 26)]
        r = _x(build_xlsx({"S": rows}))

        chunks = _chunks(r)
        assert len(chunks) == 3               # 25 lignes / 10
        assert [c["meta"]["chunk_of"] for c in chunks] == [1, 2, 3]
        assert all(c["meta"]["chunk_total"] == 3 for c in chunks)

        # Plages contiguës et DISJOINTES : aucune ligne dupliquée (overlap = 0).
        assert chunks[0]["meta"]["row_start"] == 2 and chunks[0]["meta"]["row_end"] == 11
        assert chunks[1]["meta"]["row_start"] == 12 and chunks[1]["meta"]["row_end"] == 21
        assert chunks[2]["meta"]["row_start"] == 22 and chunks[2]["meta"]["row_end"] == 26

        # Chaque projet n'apparaît QU'UNE fois dans tout le classeur : un comptage
        # resterait juste. C'est la raison du chevauchement nul.
        text = _all_text(r)
        for i in range(1, 26):
            assert text.count(f"| P{i} |") == 1

    def test_headers_repeated_in_every_chunk(self, settings):
        settings.DOCUMENT_EXCEL_ROWS_PER_CHUNK = 5
        rows = [["Projet", "Budget"]] + [[f"P{i}", i] for i in range(1, 13)]
        r = _x(build_xlsx({"S": rows}))
        for c in _chunks(r):
            assert "| Projet | Budget |" in c["text"]
            assert "Feuille : S" in c["text"]

    def test_char_budget_binds_before_row_budget(self, settings):
        settings.DOCUMENT_EXCEL_ROWS_PER_CHUNK = 40
        settings.DOCUMENT_EXCEL_CHUNK_MAX_CHARS = 400
        rows = [["Projet", "Note"]] + [[f"P{i}", "x" * 120] for i in range(1, 21)]
        r = _x(build_xlsx({"S": rows}))
        chunks = _chunks(r)
        assert len(chunks) > 1                         # la borne CHARS a mordu
        assert all(c["meta"]["row_end"] - c["meta"]["row_start"] + 1 < 40 for c in chunks)

    def test_wide_table_falls_back_to_key_value(self, settings):
        settings.DOCUMENT_EXCEL_MAX_COLUMNS_MARKDOWN = 5
        headers = [f"c{i}" for i in range(1, 9)]
        r = _x(build_xlsx({"S": [headers, list(range(1, 9))]}))
        c = _chunks(r)[0]
        assert "excel_wide_table_fallback" in r.warnings
        assert "c1: 1 ; c2: 2" in c["text"]
        assert "| c1 |" not in c["text"]               # pas de Markdown

    def test_max_chunks_marks_partial(self, settings):
        settings.DOCUMENT_EXCEL_ROWS_PER_CHUNK = 1
        settings.DOCUMENT_EXCEL_MAX_CHUNKS = 3
        rows = [["Projet", "Budget"]] + [[f"P{i}", i] for i in range(1, 20)]
        r = _x(build_xlsx({"S": rows}))
        assert len(_chunks(r)) == 3
        assert r.partial is True
        assert r.status.value == "partial"             # jamais présenté comme un succès
        assert "excel_max_chunks_reached" in r.warnings
        assert r.metadata["excel"]["truncated"] is True

    def test_too_many_cells_rejected(self, settings):
        settings.DOCUMENT_EXCEL_MAX_CELLS = 10
        with pytest.raises(ExtractionLimitExceeded) as ei:
            _x(build_xlsx({"S": [[i, i, i, i, i] for i in range(10)]}))
        assert ei.value.code == "excel_too_many_cells"


# --------------------------------------------------------------------------- #
# Contenu non tabulaire & structure
# --------------------------------------------------------------------------- #
class TestNonTabular:
    def test_merged_cells_are_reported_not_invented(self):
        body = (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Titre fusionne</t></is></c></row>'
            '<row r="2">'
            '<c r="A2" t="inlineStr"><is><t>Projet</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>Budget</t></is></c></row>'
            '<row r="3">'
            '<c r="A3" t="inlineStr"><is><t>Atlas</t></is></c>'
            '<c r="B3"><v>100</v></c></row>'
        )
        sheet = raw_sheet(body, dimension="A1:C3").replace(
            "</sheetData>", '</sheetData><mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells>'
        )
        r = _x(make_raw_xlsx(sheet))
        assert "excel_merged_cells_detected" in r.warnings
        assert r.metadata["excel"]["merged_ranges"] == ["Feuil1!A1:C1"]
        # Aucune propagation inventée sur B1/C1.
        assert _all_text(r).count("Titre fusionne") == 1

    def test_charts_and_images_are_counted_never_parsed(self):
        r = _x(make_raw_xlsx(
            raw_sheet('<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'),
            extra={"xl/charts/chart1.xml": "<c/>", "xl/media/image1.png": b"\x89PNG"},
        ))
        ex = r.metadata["excel"]
        assert ex["charts"] == 1 and ex["images"] == 1
        assert "excel_visual_content_ignored" in r.warnings

    def test_external_links_detected_never_followed(self):
        r = _x(make_raw_xlsx(
            raw_sheet('<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'),
            extra={"xl/externalLinks/externalLink1.xml": "<externalLink/>"},
        ))
        assert r.metadata["excel"]["external_links"] == 1
        assert "excel_external_links_detected" in r.warnings

    def test_ole_objects_detected_never_opened(self):
        r = _x(make_raw_xlsx(
            raw_sheet('<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'),
            extra={"xl/embeddings/oleObject1.bin": b"\xd0\xcf\x11\xe0"},
        ))
        assert r.metadata["excel"]["ole_objects"] == 1
        assert "excel_ole_objects_detected" in r.warnings

    def test_corrupted_workbook_fails_cleanly(self):
        with pytest.raises((CorruptedFile, RejectedFile)):
            _x(b"PK\x03\x04" + b"\x00" * 60)
