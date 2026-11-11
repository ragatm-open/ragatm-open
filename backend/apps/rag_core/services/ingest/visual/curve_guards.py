"""Garde-fous de sécurité numérique du shadow des courbes.

Le lot 15 a montré, sur vérité synthétique, deux façons pour le moteur figé (``curve_digitization``) de
rendre une valeur FAUSSE avec le statut fiable. Ce module ne change aucun calcul du moteur : il décide
seulement quand une mesure ne peut pas être garantie.

* ``right_y_axis`` (AVANT la mesure, OCR seul) — un second axe vertical, à droite du tracé : la série
  peut appartenir à l'une ou l'autre échelle, et le moteur ne lit que l'échelle gauche. Une colonne d'au
  moins ``RIGHT_AXIS_MIN_TICKS`` graduations numériques alignées, de valeurs distinctes, décroissantes vers
  le bas et réparties verticalement -> ``UNSUPPORTED`` (``dual_y_axis_unsupported``) pour tout le
  graphique. Deux graduations seulement dans cette configuration : l'axe ne peut pas être écarté ->
  ``NOT_DETERMINABLE`` (``possible_right_y_axis``). Un nombre isolé, une légende textuelle, une rangée
  d'étiquettes X (même hauteur) ne forment pas une colonne et ne déclenchent rien.

* ``neutral_axis_grid`` (APRÈS une mesure ``MEASURED_FROM_PIXELS``) — une série de couleur NEUTRE (gris,
  noir) peut se confondre avec un axe ou une ligne de quadrillage. On repère les lignes STRUCTURELLES du
  tracé : rangées ou colonnes droites où des pixels neutres non blancs couvrent au moins
  ``STRUCT_LINE_FRAC`` de la largeur (hauteur). Une telle ligne est ambiguë si sa couleur entre dans le
  masque de la série (distance < ``COLOR_TOL`` du moteur). Si l'extremum mesuré tombe sur une ligne ambiguë
  (à la demi-épaisseur du trait + 2 px près) : ``UNRELIABLE`` (``neutral_series_axis_grid_ambiguity``),
  la valeur ne reste que diagnostique. Une série neutre trop proche du fond blanc :
  ``NOT_DETERMINABLE`` (``neutral_series_background_ambiguity``). Les séries colorées ne sont jamais
  concernées : un axe ou un quadrillage neutre n'entre pas dans leur masque.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from . import curve_digitization as CD

#: Écart max-min des canaux sous lequel une couleur est « neutre » (gris, noir).
NEUTRAL_MAX_SPREAD = 24
#: Part minimale de la largeur (hauteur) du tracé couverte pour qu'une rangée (colonne) soit structurelle.
STRUCT_LINE_FRAC = 0.6
#: Pixel « non blanc » : au moins un canal sous cette valeur.
NON_WHITE_MAX = 245
#: Marge autour d'une ligne structurelle, en plus de la demi-épaisseur du trait (px).
STRUCT_MARGIN_PX = 2
RIGHT_AXIS_MIN_TICKS = 3
RIGHT_AXIS_ALIGN_PX = 12
RIGHT_AXIS_ZONE_FRAC = 0.75

NEUTRAL_REASON = "neutral_series_axis_grid_ambiguity"
BACKGROUND_REASON = "neutral_series_background_ambiguity"
DUAL_AXIS_REASON = "dual_y_axis_unsupported"
POSSIBLE_DUAL_AXIS_REASON = "possible_right_y_axis"


# --------------------------------------------------------------------------- #
# Garde 2 : second axe Y à droite (OCR seul)
# --------------------------------------------------------------------------- #
def right_y_axis(response: dict) -> Optional[tuple]:
    """``None`` si aucun axe droit n'est indiqué, sinon ``(statut, motif, détail)``."""
    lines = CD._ocr(response)
    item = (response.get("items") or [{}])[0]
    region = item.get("region_bbox")
    if not lines or not region:
        return None
    left, _, right, _ = [float(v) for v in region]
    zone = left + RIGHT_AXIS_ZONE_FRAC * max(right - left, 1.0)
    cands = [(CD.parse_tick(t), (b[1] + b[3]) / 2, b) for t, s, b in lines
             if s >= CD.OCR_MIN_SCORE and CD.parse_tick(t) is not None and b[0] >= zone]
    if len(cands) < 2:
        return None
    lefts = np.array([c[2][0] for c in cands])
    med = float(np.median(lefts))
    col = [c for c in cands if abs(c[2][0] - med) <= RIGHT_AXIS_ALIGN_PX]
    # Une valeur par hauteur : une rangée d'étiquettes X (même y) n'est pas une colonne d'axe.
    pts = []
    for v, y, b in sorted(col, key=lambda c: c[1]):
        if pts and y - pts[-1][1] < 0.5 * max(b[3] - b[1], 1.0):
            continue
        pts.append((v, y))
    values = [p[0] for p in pts]
    if len(pts) < 2 or len(set(values)) < 2:
        return None
    decreasing_downwards = all(a > b for a, b in zip(values, values[1:]))
    if not decreasing_downwards:
        return None
    detail = f"{len(pts)} graduations alignées à droite ({min(values):g}…{max(values):g})"
    if len(pts) >= RIGHT_AXIS_MIN_TICKS:
        return ("UNSUPPORTED", DUAL_AXIS_REASON, detail)
    return ("NOT_DETERMINABLE", POSSIBLE_DUAL_AXIS_REASON, detail)


# --------------------------------------------------------------------------- #
# Garde 1 : série neutre / axe / quadrillage (après la mesure)
# --------------------------------------------------------------------------- #
def is_neutral(color) -> bool:
    c = np.asarray(color, dtype=float)
    return float(c.max() - c.min()) <= NEUTRAL_MAX_SPREAD


def structural_lines(img: np.ndarray, box: list) -> tuple:
    """Rangées et colonnes structurelles du tracé (indices relatifs), avec leur couleur médiane."""
    x0, y0, x1, y1 = [int(v) for v in box]
    sub = img[max(y0, 0):y1, max(x0, 0):x1].astype(int)
    if sub.size == 0:
        return [], []
    spread = sub.max(axis=2) - sub.min(axis=2)
    neutral = (spread <= NEUTRAL_MAX_SPREAD) & (sub.min(axis=2) < NON_WHITE_MAX)
    rows = [(int(r), np.median(sub[r][neutral[r]], axis=0)) for r in np.where(neutral.mean(axis=1) >= STRUCT_LINE_FRAC)[0]]
    cols = [(int(c), np.median(sub[:, c][neutral[:, c]], axis=0)) for c in np.where(neutral.mean(axis=0) >= STRUCT_LINE_FRAC)[0]]
    return rows, cols


def neutral_axis_grid(result, img: np.ndarray):
    """Rend ``result`` inchangé, ou un ``CD.Result`` d'abstention si l'extremum peut être un axe/une grille."""
    if result.status != "MEASURED_FROM_PIXELS":
        return result
    diag = result.diagnostics or {}
    color, box = diag.get("color"), diag.get("plot_box")
    if color is None or box is None or result.x_pixel is None or not is_neutral(color):
        return result
    color = np.asarray(color, dtype=float)
    if np.linalg.norm(255.0 - color) < CD.COLOR_TOL:
        return CD.Result("NOT_DETERMINABLE", f"{BACKGROUND_REASON} : série neutre trop proche du fond",
                         value=result.value, diagnostics=diag)
    rows, cols = structural_lines(img, box)
    ambiguous_rows = [r for r, c in rows if np.linalg.norm(c - color) < CD.COLOR_TOL]
    ambiguous_cols = [k for k, c in cols if np.linalg.norm(c - color) < CD.COLOR_TOL]
    margin = float(diag.get("line_thickness_px") or 1.0) / 2.0 + STRUCT_MARGIN_PX
    ye, xe = float(result.y_pixel) - box[1], float(result.x_pixel) - box[0]
    hit_row = any(abs(ye - r) <= margin for r in ambiguous_rows)
    hit_col = any(abs(xe - k) <= margin for k in ambiguous_cols)
    if hit_row or hit_col:
        where = "ligne horizontale" if hit_row else "ligne verticale"
        return CD.Result("UNRELIABLE", f"{NEUTRAL_REASON} : extremum sur une {where} d'axe/quadrillage de même couleur",
                         value=result.value, diagnostics={**diag, "structural_rows": len(rows), "structural_cols": len(cols),
                                                          "ambiguous_rows": len(ambiguous_rows), "ambiguous_cols": len(ambiguous_cols)})
    return result
