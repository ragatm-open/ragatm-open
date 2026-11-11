"""Dimension FANTÔME : le garde « trop de cellules » ne doit plus refuser un classeur
minuscule qui déclare une plage aberrante.

Défaut reproduit sur des classeurs synthétiques de reporting. La balise
`<dimension>` d'un `.xlsx` déclare couramment `A1:XFD671` — XFD est la 16384ᵉ et
DERNIÈRE colonne d'Excel — alors que la feuille ne contient qu'une poignée de cellules
réelles. Mesuré sur un fichier réel : 10 996 469 cellules déclarées, 5 733 réelles
(1918× de sur-déclaration). Le garde pré-ouverture refusait ce classeur de 5 733 cellules
comme « trop volumineux ».

Cette balise est celle-là même qu'on a appris à NE PAS croire (`reset_dimensions`). On ne
refuse donc plus dessus quand la largeur est aberrante : on défère au comptage des
cellules RÉELLES pendant l'extraction en flux, qui, lui, ne ment pas et s'arrête au seuil.
"""
import pytest

from apps.rag_core.services.ingest.extraction import config
from apps.rag_core.services.ingest.extraction.base import ExtractionLimitExceeded
from apps.rag_core.services.ingest.extraction.excel_workbook import inspect_workbook

from ._builders import make_raw_xlsx, raw_sheet


def _phantom_workbook():
    """Feuille minuscule (1 cellule réelle) déclarant `A1:XFD671` (~11 M cellules)."""
    body = '<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'
    return make_raw_xlsx(raw_sheet(body, dimension="A1:XFD671"))


def _honest_wide_workbook(dimension):
    body = '<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'
    return make_raw_xlsx(raw_sheet(body, dimension=dimension))


class TestThePhantomDimensionNoLongerRefuses:
    def test_a_phantom_full_width_dimension_passes_the_pre_open_guard(self, settings):
        settings.DOCUMENT_EXCEL_MAX_CELLS = 1_000_000
        inv = inspect_workbook(_phantom_workbook())      # ne doit PAS lever
        assert "excel_phantom_dimension_ignored" in inv.warnings

    def test_the_declared_width_is_recorded(self):
        inv = inspect_workbook(_phantom_workbook())
        assert inv.sheets[0].declared_cols == 16384       # XFD

    def test_a_genuinely_large_but_plausible_sheet_is_still_refused(self, settings):
        """Le correctif ne rescape QUE les fantômes : une feuille réellement grande
        (largeur plausible) reste refusée — c'est le rôle du garde."""
        settings.DOCUMENT_EXCEL_MAX_CELLS = 1_000_000
        # 22 colonnes × 100 000 lignes = 2,2 M cellules, largeur 22 = crédible.
        with pytest.raises(ExtractionLimitExceeded) as e:
            inspect_workbook(_honest_wide_workbook("A1:V100000"))
        assert e.value.code == "excel_too_many_cells"

    def test_a_moderately_wide_sheet_under_the_phantom_cap_is_still_counted(self, settings):
        """La borne est généreuse : une table de taux large (≤ cap) reste comptée."""
        settings.DOCUMENT_EXCEL_MAX_CELLS = 1_000_000
        settings.DOCUMENT_EXCEL_MAX_DECLARED_COLS = 2048
        # 200 colonnes × 10 000 lignes = 2 M : largeur crédible (< 2048) -> refusé.
        with pytest.raises(ExtractionLimitExceeded):
            inspect_workbook(_honest_wide_workbook("A1:GR10000"))  # GR = 200

    def test_the_cap_is_configurable(self, settings):
        settings.DOCUMENT_EXCEL_MAX_CELLS = 1_000_000
        settings.DOCUMENT_EXCEL_MAX_DECLARED_COLS = 8            # très strict
        # Largeur 22 > 8 -> jugée fantôme -> déférée -> pas de refus.
        inv = inspect_workbook(_honest_wide_workbook("A1:V100000"))
        assert "excel_phantom_dimension_ignored" in inv.warnings


class TestTheRealCellGuardStillProtects:
    def test_extraction_reads_only_the_real_used_range(self, settings):
        """La preuve que déférer est SÛR : `reset_dimensions` borne la lecture au réel."""
        from apps.rag_core.services.ingest.extraction import extract_document

        settings.DOCUMENT_EXCEL_MAX_CELLS = 1_000_000
        r = extract_document(_phantom_workbook(), filename="phantom.xlsx")
        # 1 cellule réelle, jamais 11 millions.
        assert r.metadata["excel"]["cells_read"] <= 2


def _workbook_with_a_chartsheet():
    """Un classeur mêlant une feuille de DONNÉES et une feuille de GRAPHIQUE.

    C'est le cas réel qui a fait surface une fois les fantômes rescapés : ces classeurs
    de reporting mêlent données et graphiques, et un `Chartsheet` n'a ni cellule ni
    `reset_dimensions`. Le sauter proprement, sans condamner tout le document."""
    import io

    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "Projet"
    ws["B1"] = "Budget"
    ws["A2"] = "Atlas"
    ws["B2"] = 250
    # Un vrai graphique (avec dessin) : c'est le cas réel. Un Chartsheet VIDE déclenche
    # un bug openpyxl (rels sans .find) sans rapport avec ce qu'on teste.
    cs = wb.create_chartsheet("Graphique")
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=2))
    cs.add_chart(chart)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestChartSheetsAreSkippedNotFatal:
    def test_a_chartsheet_does_not_crash_the_document(self):
        from apps.rag_core.services.ingest.extraction import extract_document

        r = extract_document(_workbook_with_a_chartsheet(), filename="mixed.xlsx")
        assert r.status.value in ("success", "partial")
        # La feuille de données est bien indexée…
        assert any(c["meta"]["sheet_name"] == "Data" for c in r.excel_chunks)
        assert "| Atlas | 250 |" in "\n".join(c["text"] for c in r.excel_chunks)
        # …et la feuille-graphique est signalée comme sautée, pas fatale.
        assert "excel_chart_sheets_skipped" in r.warnings
        assert not any(c["meta"]["sheet_name"] == "Graphique" for c in r.excel_chunks)
