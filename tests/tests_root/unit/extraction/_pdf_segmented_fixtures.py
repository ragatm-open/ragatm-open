"""NATIVE-TABLE-GEOMETRY-12 — PDF SYNTHÉTIQUES : grilles à filets de colonnes SEGMENTÉS.

Propriétés reproduites (sans aucune donnée réelle) : filets dessinés en RECTANGLES FINS (deux bords par filet),
bandeau d'en-tête réglé, rangées de section qui interrompent les filets intérieurs, rangée de total qui fusionne
les deux premières colonnes (le premier filet intérieur s'y interrompt seul), filets horizontaux PARTIELS sur les
colonnes de libellé, bords continus. Vocabulaire neutre ; repère « top » comme pdfplumber.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from . import _pdf_header_band_fixtures as B

EDGES = [60.0, 110.0, 290.0, 330.0, 400.0, 470.0]
HEADER = [["Code", "Designation", "Count", "Mean area", "Total area"]]
ROW_H = 16.0
SECTION_H = 14.0
LINE = 8.0


def _vrule(c, x: float, top: float, bottom: float, thin: bool) -> None:
    if bottom - top <= 0:
        return
    if thin:
        c.rect(x - 0.35, B._y(bottom), 0.7, bottom - top, fill=1, stroke=0)
    else:
        B._vline(c, x, top, bottom)


def _hrule(c, y: float, x0: float, x1: float, thin: bool) -> None:
    if thin:
        c.rect(x0, B._y(y) - 0.35, x1 - x0, 0.7, fill=1, stroke=0)
    else:
        B._hline(c, y, x0, x1)


def data_rows(n: int, start: int = 0, labels: Optional[Sequence[str]] = None) -> List[List[str]]:
    rows = []
    for i in range(n):
        k = start + i
        label = labels[i] if labels else f"Room {chr(65 + k % 26)}{k}"
        mean = f"{2 + k},{(k * 7) % 100:02d}"
        rows.append([str(k), label, str(1 + k % 4), mean, f"{(2 + k) * (1 + k % 4)},{(k * 3) % 100:02d}"])
    return rows


def segmented_table(c, *, top: float = 110.0, edges: Sequence[float] = EDGES,
                    header: Sequence[Sequence[str]] = HEADER,
                    sections: Sequence = (("Shared rooms", data_rows(5)),),
                    total_rows: bool = True, thin: bool = True, partial_hrules: bool = False,
                    border_end: Optional[float] = None, header_rule_missing: Optional[int] = None,
                    body_shift: float = 0.0, foreign_rule: Optional[float] = None,
                    straddle_row: Optional[int] = None, between_value_row: Optional[int] = None,
                    backgrounds: bool = False) -> float:
    """Dessine une grille segmentée ; rend l'ordonnée de fin. `sections` : (libellé de section, rangées).
    `backgrounds` : fonds gris pleine largeur du bandeau, des rangées de section et de total (comme le cas réel :
    l'analyseur y voit UN fragment pleine page, rejeté `single_semantic_column`)."""
    left, right = edges[0], edges[-1]
    inner = list(edges[1:-1])
    y = top
    # Bandeau d'en-tête : tous les filets intérieurs y forment un segment commun.
    band_top = y
    band_bot = y + 6.0 + LINE * max(len(h) for h in header) + 4.0

    def fill(t: float, b: float) -> None:
        if backgrounds:
            c.setFillGray(0.92)
            c.rect(left, B._y(b), right - left, b - t, fill=1, stroke=0)
            c.setFillGray(0.0)

    fill(band_top, band_bot)
    for level, labels in enumerate(header):
        for j, text in enumerate(labels):
            for k, part in enumerate(text.split("\n")):
                B._text(c, edges[j] + 2, band_top + 4 + (level + k) * LINE, part)
    for j, x in enumerate(inner):
        if header_rule_missing is not None and j == header_rule_missing:
            continue
        _vrule(c, x, band_top, band_bot, thin)
    y = band_bot
    for s_index, (section, rows) in enumerate(sections):
        # Rangée de section : filets intérieurs interrompus, libellé dans la colonne de désignation.
        fill(y, y + SECTION_H)
        B._text(c, edges[1] + 2, y + 3, section)
        y += SECTION_H
        seg_top = y
        for r, row in enumerate(rows):
            for j, cell in enumerate(row):
                x = edges[j] + 2
                if straddle_row == r and j == 1:
                    x = edges[2] - 12  # libellé qui chevauche le filet suivant
                if between_value_row == r and j == len(row) - 1:
                    x = edges[-2] - 8  # valeur à cheval sur deux colonnes
                B._text(c, x, y + 4, cell)
            y += ROW_H
            if partial_hrules:
                _hrule(c, y, edges[0], edges[2], thin)
        for x in inner:
            _vrule(c, x + body_shift, seg_top, y, thin)
        if total_rows:
            # Rangée de total : colonnes 0-1 fusionnées (le 1er filet intérieur s'interrompt seul).
            fill(y, y + ROW_H)
            B._text(c, edges[1] + 20, y + 4, f"Total {section}")
            B._text(c, edges[-2] + 2, y + 4, f"{100 + s_index},00")
            for x in inner[1:]:
                _vrule(c, x + body_shift, y, y + ROW_H, thin)
            y += ROW_H
    end = y if border_end is None else border_end
    _vrule(c, left, band_top, end, thin)
    _vrule(c, right, band_top, end, thin)
    if foreign_rule is not None:
        _vrule(c, foreign_rule, band_bot + SECTION_H, y, thin)
    return y


def page(**kw) -> bytes:
    return B._pdf([lambda c, _p: segmented_table(c, **kw)])


def two_tables(*, gap: float = 0.0, continuous_rules: bool = True) -> bytes:
    """Deux tableaux superposés aux colonnes alignées ; le second a son PROPRE en-tête (autres libellés)."""
    def draw(c, _p):
        end = segmented_table(c, top=110.0, sections=(("Shared rooms", data_rows(4)),), total_rows=False)
        top2 = end + gap
        header2 = [["Ref", "Item", "Qty", "Unit price", "Amount"]]
        segmented_table(c, top=top2, header=header2, sections=(("Equipment", data_rows(4, start=20)),), total_rows=False)
        if continuous_rules and gap > 0:
            for x in EDGES:
                _vrule(c, x, end, top2, True)
    return B._pdf([draw])


def with_cartouche_and_legend() -> bytes:
    def draw(c, p):
        B.furniture(c, page=p)
        segmented_table(c, top=110.0)
        B._text(c, EDGES[-1] + 12, 180.0, "Legend 4,50")
    return B._pdf([draw])
