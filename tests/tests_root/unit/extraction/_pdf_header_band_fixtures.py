"""NATIVE-TABLE-RECOVERY-2 — PDF SYNTHÉTIQUES : bandeaux d'en-tête réglés et corps sans en-tête.

Vocabulaire neutre (`ID`, `Description`, `Qty`, `Unit`, `Ref`) : aucun terme du corpus de
qualification. Les PDF sont DESSINÉS au canevas, filet par filet, parce que la propriété testée
est géométrique : quels filets existent, où ils commencent et finissent. Un générateur de
tableaux (platypus) les dessinerait tous et masquerait précisément les cas utiles.

Repère : A4 portrait, coordonnées données en « top » (distance au HAUT de la page, comme
pdfplumber) et converties pour reportlab.

Deux primitives reproduisent les constats réels de NATIVE-TABLE-GUARD-1R :

- `furniture` : un cartouche réglé en tête de CHAQUE page, que l'analyseur reconnaît comme
  un petit tableau et qui s'intercale entre les morceaux d'un même corps ;
- `skew=True` : glyphes très légèrement cisaillés (invisibles à l'œil) — ``pdfplumber`` les
  tient pour non droits et découpe les mots lettre à lettre, comme sur la page réelle.
"""
from __future__ import annotations

from io import BytesIO
from typing import List, Optional, Sequence

from reportlab.pdfgen import canvas

W, H = 595.0, 842.0
#: Bords de colonnes du corps : 5 colonnes de largeurs inégales (étroites et larges).
EDGES = [40.0, 70.0, 250.0, 330.0, 380.0, 460.0]
SCHEMA = ["ID", "Description", "Qty", "Unit", "Ref"]
ROW_H = 14.0


def body_rows(n: int, *, start: int = 1) -> List[List[str]]:
    """Lignes de corps : identifiant, libellé, deux nombres et une unité — le corps est
    majoritairement numérique, donc sans rangée d'en-tête détectable."""
    return [[str(start + i), f"Item {start + i}", f"{(start + i) * 1.5:.1f}".replace(".", ","),
             "m", str(4700 + start + i)] for i in range(n)]


def _y(top: float) -> float:
    return H - top


def _text(c, x: float, top: float, s: str, *, size: float = 7.0, skew: bool = False) -> None:
    t = c.beginText()
    if skew:
        # Cisaillement de 0,05 % : invisible, mais les glyphes ne sont plus « droits ».
        t.setTextTransform(1.0, 0.0005, 0.0005, 1.0, x, _y(top + size))
    else:
        t.setTextOrigin(x, _y(top + size))
    t.setFont("Helvetica", size)
    t.textLine(s)
    c.drawText(t)


def _vline(c, x: float, top: float, bottom: float) -> None:
    c.line(x, _y(top), x, _y(bottom))


def _hline(c, y: float, x0: float, x1: float) -> None:
    c.line(x0, _y(y), x1, _y(y))


def furniture(c, *, page: int) -> None:
    """Cartouche réglé (2 rangées x 4 colonnes) en tête de page, de y=38 à y=66."""
    xs = [126.0, 165.0, 451.0, 490.0, 543.0]
    rows = [["Project :", "Alpha building", "Date :", "2026"], ["Doc :", "Report", "Page :", str(page)]]
    for i, row in enumerate(rows):
        top = 38.0 + i * 14.0
        for j, cell in enumerate(row):
            _text(c, xs[j] + 2, top + 3, cell)
    for x in xs:
        _vline(c, x, 38.0, 66.0)
    for y in (38.0, 52.0, 66.0):
        _hline(c, y, xs[0], xs[-1])


def grid_body(c, rows: Sequence[Sequence[str]], *, top: float, edges: Sequence[float] = EDGES,
              separators: Sequence[int] = (), skew: bool = False) -> float:
    """Corps entièrement réglé (détectable). `separators` : indices de rangées pleine largeur.
    Renvoie le bas du corps."""
    y = top
    for i, row in enumerate(rows):
        if i in separators:
            _text(c, edges[0] + 2, y + 3, row[0], skew=skew)
            _vline(c, edges[0], y, y + ROW_H)
            _vline(c, edges[-1], y, y + ROW_H)
        else:
            for j, cell in enumerate(row):
                if cell:
                    _text(c, edges[j] + 2, y + 3, cell, skew=skew)
            for x in edges:
                _vline(c, x, y, y + ROW_H)
        _hline(c, y, edges[0], edges[-1])
        y += ROW_H
    _hline(c, y, edges[0], edges[-1])
    return y


def ruled_band(c, labels: Sequence[Sequence[str]], *, top: float, edges: Sequence[float] = EDGES,
               spans: Optional[Sequence[tuple]] = None, horizontal: bool = True,
               level_h: float = 14.0) -> float:
    """Bandeau d'en-tête réglé. `labels[k]` : textes du niveau k, une entrée par colonne
    (chaîne vide = rien). `spans` : (niveau, première colonne, dernière colonne) d'une
    cellule de niveau supérieur qui COUVRE plusieurs colonnes (pas de filet intérieur à ce
    niveau). `horizontal=False` : filets verticaux seulement (bandeau non reconnu comme
    tableau par l'analyseur, comme sur la page réelle). Renvoie le bas du bandeau."""
    spans = list(spans or [])
    n_levels = len(labels)
    bottom = top + n_levels * level_h
    for k, level in enumerate(labels):
        ltop = top + k * level_h
        for j, label in enumerate(level):
            if label:
                _text(c, edges[j] + 2, ltop + 3, label)
    for i, x in enumerate(edges):
        # Un filet intérieur couvert par une cellule fusionnée ne descend qu'à partir
        # du niveau qui suit la fusion.
        start = top
        for (lv, a, b) in spans:
            if a < i <= b:
                start = max(start, top + (lv + 1) * level_h)
        _vline(c, x, start, bottom)
    if horizontal:
        _hline(c, top, edges[0], edges[-1])
        _hline(c, bottom, edges[0], edges[-1])
    return bottom


def _pdf(draw_pages) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    for i, draw in enumerate(draw_pages, start=1):
        draw(c, i)
        c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Même page
# --------------------------------------------------------------------------- #
def band_same_page(*, labels=None, gap: float = 4.0, shift: float = 0.0, spans=None,
                   band_edges=None, n_rows: int = 10, separators=()) -> bytes:
    """Bandeau réglé puis corps sans en-tête, juste dessous. `shift` décale les bords du
    corps ; `band_edges` donne au bandeau une grille propre (colonnes fusionnées…)."""
    labels = labels if labels is not None else [SCHEMA]
    body_edges = [e + shift for e in EDGES]
    rows = body_rows(n_rows)
    for i in separators:
        rows.insert(i, ["Group " + str(i), "", "", "", ""])

    def draw(c, _page):
        bottom = ruled_band(c, labels, top=120.0, edges=band_edges or EDGES, spans=spans)
        grid_body(c, rows, top=bottom + gap, edges=body_edges, separators=separators)
    return _pdf([draw])


def body_only() -> bytes:
    return _pdf([lambda c, _p: grid_body(c, body_rows(10), top=120.0)])


def band_only() -> bytes:
    return _pdf([lambda c, _p: ruled_band(c, [SCHEMA], top=120.0)])


def adjacent_tables() -> bytes:
    """Bandeau + corps 1, puis un second corps INDÉPENDANT plus bas, mêmes colonnes."""
    def draw(c, _page):
        bottom = ruled_band(c, [SCHEMA], top=120.0)
        end = grid_body(c, body_rows(6), top=bottom + 4.0)
        grid_body(c, body_rows(6, start=50), top=end + 60.0)
    return _pdf([draw])


# --------------------------------------------------------------------------- #
# Page précédente (tableau paginé titré sur sa première page seulement)
# --------------------------------------------------------------------------- #
LEAD_ROW = ["9", "Gizmo beta", "3,5", "kg", "4811"]


def band_previous_page(*, skew: bool = False, with_furniture: bool = True,
                       grid_reaches_bottom: bool = True, edges_page2=None, straddle: bool = False) -> bytes:
    """Page 1 : bandeau réglé (filets verticaux seulement) puis lignes de corps NON réglées
    horizontalement — l'analyseur ne reconnaît pas la grille ; la ligne `LEAD_ROW` y figure.
    `straddle=True` : un mot chevauche un filet de colonne (grille non certifiable).
    Pages 2 et 3 : corps réglé, sans en-tête, mêmes colonnes, coupé en bas de page 2."""
    def page1(c, page):
        if with_furniture:
            furniture(c, page=page)
        bottom = ruled_band(c, [SCHEMA], top=123.0, horizontal=False)
        end = 720.0 if grid_reaches_bottom else 400.0
        y = bottom + 10.0
        for row in body_rows(8, start=100)[:4] + [LEAD_ROW]:
            for j, cell in enumerate(row):
                _text(c, EDGES[j] + 2, y + 3, cell, skew=skew)
            y += ROW_H
        if straddle:
            # Un mot à cheval sur le filet Description|Qty : colonnes non démontrées pour ce corps.
            _text(c, EDGES[2] - 12, y + 3, "spillover", skew=skew)
        for x in EDGES:
            _vline(c, x, bottom + 8.0, end)
        _vline(c, EDGES[0], 123.0, end)
        _vline(c, EDGES[-1], 123.0, end)

    def page2(c, page):
        if with_furniture:
            furniture(c, page=page)
        grid_body(c, body_rows(46), top=99.0, edges=edges_page2 or EDGES)

    def page3(c, page):
        if with_furniture:
            furniture(c, page=page)
        grid_body(c, body_rows(20, start=47), top=99.0)
    return _pdf([page1, page2, page3])
