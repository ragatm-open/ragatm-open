"""NATIVE-TABLE-GROUNDING-3 — PDF SYNTHÉTIQUES : grilles à colonnes RÉGLÉES sans filets horizontaux.

Calqué sur une page réelle de tableau de prix observée en qualification : filets verticaux dessinés à chaque bord de
colonne, interrompus ensemble aux rangées de section ; aucun filet entre les rangées ; cellules
multilignes CENTRÉES verticalement sur la ligne qui porte les cellules d'une seule ligne.
Vocabulaire neutre ; repère « top » comme pdfplumber (cf. `_pdf_header_band_fixtures`).
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from . import _pdf_header_band_fixtures as B

LINE = 9.0          # pas entre deux lignes d'une même cellule
ROW_GAP = 4.0       # espace entre deux rangées


def _cell_lines(cell) -> List[str]:
    return list(cell) if isinstance(cell, (list, tuple)) else ([cell] if cell else [])


def ruleless_page(c, rows: Sequence[Sequence], *, header: Optional[Sequence[Sequence[str]]] = None,
                  edges: Sequence[float] = B.EDGES, top: float = 120.0, groups: Sequence[str] = ("Group 1",),
                  group_at: Sequence[int] = (), align: str = "center", straddle_at: Optional[int] = None,
                  header_spans=(), skew: bool = False) -> float:
    """Dessine une grille. `rows[i][j]` : texte, ou liste de lignes (cellule multiligne).
    `header` : niveaux du bandeau (None = pas de bandeau, filets continus dès le haut).
    `groups` / `group_at` : libellé de section placé avant la rangée d'indice `group_at[k]`
    (le premier libellé précède toujours la première rangée). `align` : « center » (ancrage)
    ou « top » (cellules multilignes alignées en haut : rangées non démontrables)."""
    left, right = edges[0], edges[-1]
    inner = edges[1:-1]
    y = top
    segments = []
    if header is not None:
        # Le bandeau trace ses propres filets (partiels pour un niveau supérieur fusionné).
        y = B.ruled_band(c, header, top=top, edges=edges, spans=header_spans, horizontal=False)
    seg_start = None
    group_idx = 0
    breaks = [0] + list(group_at)
    for i, row in enumerate(rows):
        if i in breaks and group_idx < len(groups):
            if seg_start is not None:
                segments.append((seg_start, y))
            B._text(c, left + 2, y + 2, groups[group_idx], skew=skew)
            y += 10.0
            group_idx += 1
            seg_start = y
        if seg_start is None:
            seg_start = y
        n = max(len(_cell_lines(cell)) for cell in row)
        height = n * LINE + ROW_GAP
        anchor = y + height / 2.0
        for j, cell in enumerate(row):
            lines = _cell_lines(cell)
            for k, text in enumerate(lines):
                if align == "center":
                    cy = anchor + (k - (len(lines) - 1) / 2.0) * LINE
                else:
                    cy = y + ROW_GAP / 2.0 + LINE / 2.0 + k * LINE
                B._text(c, edges[j] + 2, cy - 4.0, text, skew=skew)
        if straddle_at == i:
            B._text(c, edges[2] - 12, anchor - 4.0 + LINE / 2.0, "spillover", skew=skew)
        y += height
    segments.append((seg_start, y))
    for x in inner:
        for a, b in segments:
            B._vline(c, x, a, b)
    B._vline(c, left, top, y)
    B._vline(c, right, top, y)
    return y


def grid(rows, **kw) -> bytes:
    return B._pdf([lambda c, _p: ruleless_page(c, rows, **kw)])


def numeric_rows(n: int, start: int = 1) -> List[List[str]]:
    return [[str(start + i), f"Item {start + i}", f"{(start + i) * 1.5:.1f}".replace(".", ","), "m",
             str(4700 + start + i)] for i in range(n)]


def multiline_rows() -> List[list]:
    return [["1", ["Long item", "first part"], "10,5", "kg", "4801"],
            ["1", "Short item", ["12,0"], "kg", "4802"],
            ["1", ["Long item", "second part", "tail"], "7,5", "kg", "4803"]]


def similar_objects_rows() -> List[list]:
    return [["1", "Pump A", "866,2", "W", "5101"], ["1", "Pump B", "912,4", "W", "5102"]]


def adjacent_grids() -> bytes:
    def draw(c, _p):
        end = ruleless_page(c, numeric_rows(3), header=[B.SCHEMA], top=120.0)
        ruleless_page(c, numeric_rows(3, start=50), header=[B.SCHEMA], top=end + 30.0)
    return B._pdf([draw])


def aligned_prose() -> bytes:
    """Texte narratif aligné en colonnes (tabulations), SANS aucun filet."""
    def draw(c, _p):
        y = 120.0
        for i in range(6):
            for j, word in enumerate(["Note", f"paragraph {i}", f"{i + 10},5", "m", str(4700 + i)]):
                B._text(c, B.EDGES[j] + 2, y, word)
            y += 13.0
    return B._pdf([draw])


def cartouche_only() -> bytes:
    return B._pdf([lambda c, p: B.furniture(c, page=p)])
