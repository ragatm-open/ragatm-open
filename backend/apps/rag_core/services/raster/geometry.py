"""Projection d'une région PDF vers les pixels d'un ``PageRaster``.

Repère SOURCE — celui des rectangles d'images de ``pdf_visual`` (PyMuPDF
``get_image_rects``), établi empiriquement contre le rendu ``ragatm-pdfium/1`` :
points PDF, page NON pivotée, origine au coin HAUT-GAUCHE de la CropBox, y vers le bas.

Repère CIBLE — celui du raster rendu (CV-3A) : pixels entiers, ``/Rotate`` déjà
appliqué par PDFium (sens horaire), origine en haut à gauche, bbox demi-ouverte.

Étapes, toutes explicites :
  1. rotation horaire du rectangle dans le cadre de la CropBox (``W × H`` points, non
     pivoté) — 90° : (x, y) → (H − y, x) ; 180° : (x, y) → (W − x, H − y) ;
     270° : (x, y) → (y, W − x) ;
  2. mise à l'échelle ``dpi / 72`` ;
  3. arrondi VERS L'EXTÉRIEUR (plancher des minimums, plafond des maximums) : la région
     entière est couverte, jamais rognée d'une ligne ;
  4. rognage au raster (une image qui déborde de la CropBox) ;
  5. rejet si la bbox résultante est vide.

Rien n'est deviné : une région invalide (non finie, inversée, hors page) rend ``None``.

Tolérance MESURÉE (rendu réel, rotations 0/90/180/270, avec et sans CropBox) : PDFium cale
le placement d'une image sur la grille de pixels du périphérique ; après rotation, l'image
rendue peut être DÉCALÉE d'au plus ``PROJECTION_TOLERANCE_PX`` sur un bord. La projection
reste la vérité géométrique ; l'appelant qui découpe un crop applique une marge
``≥ PROJECTION_TOLERANCE_PX`` (``crops.pad_bbox``) — ``VISUAL_CROP_PADDING_PX`` ci-dessous.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

BBox = Tuple[int, int, int, int]
_ROTATIONS = (0, 90, 180, 270)

#: Écart maximal mesuré entre la projection et les pixels rendus par ``ragatm-pdfium/1``.
PROJECTION_TOLERANCE_PX = 1
#: Marge appliquée au crop d'une région projetée (contexte visuel + tolérance ci-dessus).
VISUAL_CROP_PADDING_PX = 8
assert VISUAL_CROP_PADDING_PX >= PROJECTION_TOLERANCE_PX


def rotate_rect_points(rect: Sequence[float], *, width_pt: float, height_pt: float,
                       rotate: int) -> Tuple[float, float, float, float]:
    """Rectangle non pivoté (origine CropBox, y vers le bas) → repère de la page affichée."""
    if rotate not in _ROTATIONS:
        raise ValueError(f"rotate must be one of {_ROTATIONS}")
    x0, y0, x1, y1 = (float(v) for v in rect)
    W, H = float(width_pt), float(height_pt)
    if rotate == 0:
        return (x0, y0, x1, y1)
    if rotate == 90:
        return (H - y1, x0, H - y0, x1)
    if rotate == 180:
        return (W - x1, H - y1, W - x0, H - y0)
    return (y0, W - x1, y1, W - x0)


def project_region_to_raster(rect: Sequence[float], *, cropbox: Sequence[float], rotate: int,
                             dpi: int, raster_width: int, raster_height: int) -> Optional[BBox]:
    """bbox pixels ``[x_min, y_min, x_max, y_max)`` dans le raster, ou ``None`` si invalide.

    ``cropbox`` : CropBox de la ``Page`` en points PDF (seules ses dimensions servent : le
    repère source est déjà relatif à son coin). ``rotate`` : ``/Rotate`` source de la page.
    """
    if rect is None or len(rect) != 4:
        return None
    try:
        vals = [float(v) for v in rect]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in vals) or not (vals[0] < vals[2] and vals[1] < vals[3]):
        return None
    width_pt = float(cropbox[2]) - float(cropbox[0])
    height_pt = float(cropbox[3]) - float(cropbox[1])
    if width_pt <= 0 or height_pt <= 0 or rotate not in _ROTATIONS:
        return None
    rx0, ry0, rx1, ry1 = rotate_rect_points(vals, width_pt=width_pt, height_pt=height_pt, rotate=rotate)
    scale = dpi / 72
    x0, y0 = math.floor(rx0 * scale), math.floor(ry0 * scale)
    x1, y1 = math.ceil(rx1 * scale), math.ceil(ry1 * scale)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(int(raster_width), x1), min(int(raster_height), y1)
    if not (x0 < x1 and y0 < y1):
        return None
    return (x0, y0, x1, y1)
