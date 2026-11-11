"""Données NATIVES des graphiques Office (DOCX, PPTX).

Un graphique Office n'est pas une image : sa partie ``chartN.xml`` (DrawingML Chart, ``c:chartSpace``) porte, pour
chaque série, le CACHE des catégories et des valeurs que le logiciel affiche (``c:strCache`` / ``c:numCache`` /
``c:*Lit``). On lit ces valeurs telles qu'enregistrées dans le fichier : aucune estimation visuelle, aucun appel au
Gateway, aucun rendu. Avant ce module, un graphique natif DOCX était ignoré et un graphique PPTX réduit à son texte
alternatif.

Limites assumées :
- le cache est la dernière valeur enregistrée par l'éditeur ; un classeur lié modifié depuis n'est pas relu ;
- les graphiques ``chartex`` (cx:, Office 2016 : cascade, entonnoir…) ne sont pas lus ;
- la mise en forme d'affichage n'est appliquée que pour le pourcentage (``formatCode`` contenant ``%``).

Bornes : séries, points et cellules plafonnés ; dépassement = troncature NOMMÉE (``truncated``), jamais silencieuse.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .xml_security import reject_deeply_nested_xml, reject_unsafe_xml

C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CHART_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"

MAX_SERIES = 50
MAX_POINTS = 500
MAX_CELLS = 5000

#: Élément de ``c:plotArea`` -> libellé du type (affiché dans le texte indexé).
CHART_TYPES = {
    "barChart": "barres", "bar3DChart": "barres", "lineChart": "courbes", "line3DChart": "courbes",
    "pieChart": "secteurs", "pie3DChart": "secteurs", "ofPieChart": "secteurs", "doughnutChart": "anneau",
    "areaChart": "aires", "area3DChart": "aires", "scatterChart": "nuage de points", "bubbleChart": "bulles",
    "radarChart": "radar", "stockChart": "boursier", "surfaceChart": "surface", "surface3DChart": "surface",
}


def _c(tag: str) -> str:
    return f"{{{C_NS}}}{tag}"


@dataclass
class ChartSeries:
    name: str
    points: List[Tuple[str, str]] = field(default_factory=list)  # (catégorie, valeur affichable)


@dataclass
class ChartData:
    chart_types: List[str]
    title: str
    series: List[ChartSeries]
    truncated: bool = False

    @property
    def point_count(self) -> int:
        return sum(len(s.points) for s in self.series)


def _texts(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    return " ".join(t.text.strip() for t in el.iter(f"{{{A_NS}}}t") if t.text and t.text.strip())


def _cache(el: Optional[ET.Element]) -> Tuple[Dict[int, str], str]:
    """``({idx: valeur}, formatCode)`` du premier cache ou littéral sous ``el``. Catégories à plusieurs niveaux :
    niveaux joints par « / », du plus général au plus fin."""
    if el is None:
        return {}, ""
    multi = el.find(f".//{_c('multiLvlStrCache')}")
    if multi is not None:
        levels = []
        for lvl in multi.findall(_c("lvl")):
            levels.append({int(p.get("idx", -1)): (p.findtext(_c("v")) or "").strip() for p in lvl.findall(_c("pt"))})
        idxs = sorted({i for lvl in levels for i in lvl if i >= 0})
        # OOXML range le niveau le plus FIN en premier.
        return {i: " / ".join(v for v in (lvl.get(i, "") for lvl in reversed(levels)) if v) for i in idxs}, ""
    for tag in ("numCache", "strCache", "numLit", "strLit"):
        cache = el.find(f".//{_c(tag)}")
        if cache is not None:
            pts = {}
            for p in cache.findall(_c("pt")):
                try:
                    idx = int(p.get("idx", -1))
                except ValueError:
                    continue
                if idx >= 0:
                    pts[idx] = (p.findtext(_c("v")) or "").strip()
            return pts, (cache.findtext(_c("formatCode")) or "")
    v = el.findtext(f".//{_c('v')}")
    return ({0: v.strip()} if v and v.strip() else {}), ""


def _display(value: str, format_code: str) -> str:
    if "%" not in (format_code or ""):
        return value
    try:
        pct = (Decimal(value) * 100).normalize()
    except (InvalidOperation, ValueError):
        return value
    text = format(pct, "f")
    return f"{text} %"


def parse_chart_xml(xml_bytes: bytes, *, part_name: str) -> Optional[ChartData]:
    """Graphique lu depuis ``chartN.xml`` ; ``None`` si aucune série exploitable. Lève ``RejectedFile`` sur un XML
    dangereux (DOCTYPE / ENTITY, imbrication), ``ET.ParseError`` sur un XML malformé."""
    reject_unsafe_xml(xml_bytes, part_name=part_name)
    reject_deeply_nested_xml(xml_bytes, part_name=part_name)
    root = ET.fromstring(xml_bytes)
    chart = root.find(_c("chart"))
    plot = chart.find(_c("plotArea")) if chart is not None else None
    if plot is None:
        return None
    title_el = chart.find(_c("title"))
    title = _texts(title_el) if title_el is not None else ""
    types: List[str] = []
    series: List[ChartSeries] = []
    truncated = False
    cells = 0
    for group in list(plot):
        local = group.tag.rsplit("}", 1)[-1]
        if local not in CHART_TYPES:
            continue
        if CHART_TYPES[local] not in types:
            types.append(CHART_TYPES[local])
        for ser in group.findall(_c("ser")):
            if len(series) >= MAX_SERIES:
                truncated = True
                break
            name_map, _ = _cache(ser.find(_c("tx")))
            name = next(iter(name_map.values()), "") or f"Série {len(series) + 1}"
            cats, _ = _cache(ser.find(_c("cat")) if ser.find(_c("cat")) is not None else ser.find(_c("xVal")))
            vals, fmt = _cache(ser.find(_c("val")) if ser.find(_c("val")) is not None else ser.find(_c("yVal")))
            pts = []
            for idx in sorted(vals):
                if len(pts) >= MAX_POINTS or cells >= MAX_CELLS:
                    truncated = True
                    break
                if vals[idx] == "":
                    continue
                pts.append((cats.get(idx) or str(idx + 1), _display(vals[idx], fmt)))
                cells += 1
            if pts:
                series.append(ChartSeries(name=name, points=pts))
    if not series:
        return None
    return ChartData(chart_types=types, title=title, series=series, truncated=truncated)


def chart_rows(chart: ChartData) -> List[List[str]]:
    """Tableau catégories × séries (première ligne = en-tête). Catégories dans l'ordre de première apparition."""
    order: List[str] = []
    for s in chart.series:
        for cat, _ in s.points:
            if cat not in order:
                order.append(cat)
    header = ["Catégorie"] + [s.name for s in chart.series]
    lookup = [dict(s.points) for s in chart.series]
    return [header] + [[cat] + [m.get(cat, "") for m in lookup] for cat in order]


def chart_caption(chart: ChartData) -> str:
    kind = ", ".join(chart.chart_types) or "graphique"
    head = f"Graphique natif ({kind}, données du fichier)"
    if chart.title:
        head += f" : {chart.title}"
    if chart.truncated:
        head += " [tronqué]"
    return head


def render_chart(chart: ChartData) -> str:
    from .pptx_render import render_table

    return chart_caption(chart) + "\n" + render_table(chart_rows(chart))
