"""graphiques NATIFS Office lus depuis leur cache de données.

Contrat :
- drapeau éteint : sortie DOCX / PPTX inchangée (PPTX : texte alternatif seul ; DOCX : rien) ;
- drapeau allumé : un tableau catégories × séries, valeurs du fichier, à la place du graphique ;
- aucun appel réseau, aucune estimation ; partie de graphique illisible ou dangereuse = comptée, le reste du fichier
  extrait normalement ; pourcentages affichés comme dans le graphique.
Graphiques SYNTHÉTIQUES générés par python-pptx (aucun fichier binaire commité).
"""
from __future__ import annotations

import io
import zipfile

import pytest

pptx = pytest.importorskip("pptx")
from pptx.chart.data import CategoryChartData, XyChartData  # noqa: E402
from pptx.enum.chart import XL_CHART_TYPE  # noqa: E402
from pptx.util import Inches  # noqa: E402

from apps.rag_core.services.ingest.extraction import office_chart as OC  # noqa: E402
from apps.rag_core.services.ingest.extraction.base import ExtractionContext  # noqa: E402
from apps.rag_core.services.ingest.extraction.docx_extractor import DocxExtractor  # noqa: E402
from apps.rag_core.services.ingest.extraction.pptx_extractor import PptxExtractor  # noqa: E402

CATS = ("2023", "2024", "2025")
SERIES = {"Ventes Nord": (12.5, 14.0, 16.25), "Ventes Sud": (9.0, 11.5, 13.0)}


def _pptx_with_chart(*, chart_type=XL_CHART_TYPE.COLUMN_CLUSTERED, title="Chiffre d'affaires", series=SERIES,
                     cats=CATS, number_format=None, alt="Graphique") -> bytes:
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Résultats annuels"
    data = CategoryChartData(number_format=number_format) if number_format else CategoryChartData()
    data.categories = list(cats)
    for name, values in series.items():
        data.add_series(name, values)
    gf = slide.shapes.add_chart(chart_type, Inches(1), Inches(1.5), Inches(6), Inches(4), data)
    gf.chart.has_title = bool(title)
    if title:
        gf.chart.chart_title.text_frame.text = title
    gf._element.nvGraphicFramePr.cNvPr.set("descr", alt)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _chart_part(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(next(n for n in z.namelist() if n.startswith("ppt/charts/chart")))


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
       '<Override PartName="/word/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/></Types>')


def _docx_with_chart(chart_xml: bytes, *, rel_target="charts/chart1.xml") -> bytes:
    drawing = ('<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
               '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
               '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
               f'<c:chart xmlns:c="{OC.C_NS}" xmlns:r="{OC.R_NS}" r:id="rIdChart1"/>'
               '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
    doc = (f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>'
           '<w:p><w:r><w:t>Avant le graphique.</w:t></w:r></w:p>' + drawing +
           '<w:p><w:r><w:t>Après le graphique.</w:t></w:r></w:p></w:body></w:document>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rIdChart1" Type="{OC.CHART_REL_TYPE}" Target="{rel_target}"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/charts/chart1.xml", chart_xml)
    return buf.getvalue()


@pytest.fixture
def charts_on(settings):
    settings.DOCUMENT_OFFICE_CHART_DATA_ENABLED = True
    return settings


@pytest.fixture
def charts_off(settings):
    settings.DOCUMENT_OFFICE_CHART_DATA_ENABLED = False
    return settings


def _pptx(data):
    return PptxExtractor().extract(data, ExtractionContext(filename="deck.pptx"))


def _docx(data):
    return DocxExtractor().extract(data, ExtractionContext(filename="note.docx"))


# --------------------------------------------------------------------------- #
# Lecture du cache
# --------------------------------------------------------------------------- #
class TestChartXml:
    def test_categories_series_values_and_title(self):
        chart = OC.parse_chart_xml(_chart_part(_pptx_with_chart()), part_name="chart1.xml")
        assert chart.chart_types == ["barres"] and chart.title == "Chiffre d'affaires"
        assert OC.chart_rows(chart) == [["Catégorie", "Ventes Nord", "Ventes Sud"],
                                        ["2023", "12.5", "9.0"], ["2024", "14.0", "11.5"], ["2025", "16.25", "13.0"]]

    @pytest.mark.parametrize("chart_type,label", [
        (XL_CHART_TYPE.LINE, "courbes"), (XL_CHART_TYPE.PIE, "secteurs"), (XL_CHART_TYPE.AREA, "aires"),
        (XL_CHART_TYPE.BAR_STACKED, "barres"), (XL_CHART_TYPE.DOUGHNUT, "anneau"), (XL_CHART_TYPE.RADAR, "radar"),
    ])
    def test_chart_families(self, chart_type, label):
        series = {"Ventes Nord": SERIES["Ventes Nord"]} if chart_type in (XL_CHART_TYPE.PIE, XL_CHART_TYPE.DOUGHNUT) else SERIES
        chart = OC.parse_chart_xml(_chart_part(_pptx_with_chart(chart_type=chart_type, series=series)), part_name="c")
        assert chart.chart_types == [label] and chart.point_count == 3 * len(series)

    def test_percent_format_is_displayed_as_in_the_chart(self):
        chart = OC.parse_chart_xml(_chart_part(_pptx_with_chart(series={"Part": (0.153, 0.2, 0.5)},
                                                                number_format="0.0%")), part_name="c")
        assert [v for _, v in chart.series[0].points] == ["15.3 %", "20 %", "50 %"]

    def test_scatter_uses_x_values_as_categories(self):
        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        data = XyChartData()
        s = data.add_series("Mesures")
        for x, y in ((1, 2.5), (2, 3.75)):
            s.add_data_point(x, y)
        slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, 0, 0, Inches(4), Inches(3), data)
        buf = io.BytesIO(); prs.save(buf)
        chart = OC.parse_chart_xml(_chart_part(buf.getvalue()), part_name="c")
        assert chart.chart_types == ["nuage de points"] and chart.series[0].points == [("1", "2.5"), ("2", "3.75")]

    def test_bounds_truncate_explicitly(self, monkeypatch):
        monkeypatch.setattr(OC, "MAX_POINTS", 2)
        chart = OC.parse_chart_xml(_chart_part(_pptx_with_chart()), part_name="c")
        assert chart.truncated and all(len(s.points) == 2 for s in chart.series)
        assert "[tronqué]" in OC.chart_caption(chart)

    def test_unsafe_xml_is_rejected(self):
        with pytest.raises(Exception):
            OC.parse_chart_xml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>', part_name="c")

    def test_no_series_is_none(self):
        xml = f'<c:chartSpace xmlns:c="{OC.C_NS}"><c:chart><c:plotArea><c:barChart/></c:plotArea></c:chart></c:chartSpace>'
        assert OC.parse_chart_xml(xml.encode(), part_name="c") is None


# --------------------------------------------------------------------------- #
# PPTX
# --------------------------------------------------------------------------- #
class TestPptx:
    def test_flag_off_keeps_alt_text_only(self, charts_off):
        res = _pptx(_pptx_with_chart(alt="Ventes par région"))
        text = res.slides[0]["text"]
        assert "[Image : Ventes par région]" in text and "Graphique natif" not in text and "16.25" not in text
        assert "native_charts" not in res.metadata["pptx"] and "native_charts" not in res.slides[0]["meta"]

    def test_flag_on_indexes_the_file_values(self, charts_on):
        res = _pptx(_pptx_with_chart(alt="Ventes par région"))
        text = res.slides[0]["text"]
        assert "Graphique natif (barres, données du fichier) : Chiffre d'affaires" in text
        assert "| 2025 | 16.25 | 13.0 |" in text and "Titre : Résultats annuels" in text
        assert res.slides[0]["meta"]["native_charts"] == 1 and res.slides[0]["meta"]["has_text"] is True
        assert res.metadata["pptx"]["native_charts"] == {"seen": 1, "extracted": 1, "failed": 0, "truncated": 0}

    def test_chart_only_slide_is_not_image_only(self, charts_on):
        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        data = CategoryChartData(); data.categories = ["A", "B"]; data.add_series("S", (1, 2))
        slide.shapes.add_chart(XL_CHART_TYPE.PIE, 0, 0, Inches(4), Inches(3), data)
        buf = io.BytesIO(); prs.save(buf)
        res = _pptx(buf.getvalue())
        assert res.slides and "pptx_image_only_slides" not in (res.warnings or [])

    def test_unreadable_chart_part_is_counted_and_falls_back(self, charts_on):
        from ._builders import _repack_pptx

        data = _pptx_with_chart(alt="Ventes par région")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.startswith("ppt/charts/chart"))
        res = _pptx(_repack_pptx(data, replace={name: b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>'}))
        assert res.metadata["pptx"]["native_charts"]["failed"] == 1
        assert "pptx_native_chart_unreadable" in res.warnings and "[Image : Ventes par région]" in res.slides[0]["text"]


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #
class TestDocx:
    def test_flag_off_ignores_the_chart(self, charts_off):
        res = _docx(_docx_with_chart(_chart_part(_pptx_with_chart())))
        assert "16.25" not in res.text and "docx_native_charts" not in res.metadata
        assert "Avant le graphique." in res.text and "Après le graphique." in res.text

    def test_flag_on_places_the_table_in_reading_order(self, charts_on):
        res = _docx(_docx_with_chart(_chart_part(_pptx_with_chart())))
        before, chart, after = (res.text.index(s) for s in ("Avant le graphique.", "Graphique natif", "Après le graphique."))
        assert before < chart < after and "| 2024 | 14.0 | 11.5 |" in res.text
        assert res.metadata["docx_native_charts"] == {"seen": 1, "extracted": 1, "failed": 0, "truncated": 0}

    @pytest.mark.parametrize("target", ["charts/missing.xml", "../../etc/passwd"])
    def test_missing_or_escaping_part_is_counted_not_fatal(self, charts_on, target):
        res = _docx(_docx_with_chart(_chart_part(_pptx_with_chart()), rel_target=target))
        assert res.metadata["docx_native_charts"]["failed"] == 1 and "docx_native_chart_unreadable" in res.warnings
        assert "Avant le graphique." in res.text
