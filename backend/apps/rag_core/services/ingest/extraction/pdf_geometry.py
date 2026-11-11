"""Géométrie des pages PDF (CV-3B) — lue telle que DÉCLARÉE par le fichier.

Lue dans l'enfant d'extraction, là où le PDF est déjà analysé par pypdf (même
bibliothèque, même ordre de pages que l'extraction de texte : ``page_number = i + 1``).
Aucun rendu, aucun raster : ``/MediaBox``, ``/CropBox`` et ``/Rotate`` sont lus dans
l'arbre des pages, attributs hérités compris (pypdf les recopie sur chaque page).

Règles (rien n'est inventé) :
- boîtes ``[x0, y0, x1, y1]`` en points PDF, coins normalisés (min/max — la norme PDF
  admet deux coins opposés quelconques) ; aucune autre transformation, et pas
  d'intersection CropBox ∩ MediaBox (le rendu la fera, cf. CV-3C) ;
- ``/CropBox`` absente → MediaBox (valeur par défaut de la norme) ;
- ``/Rotate`` normalisé modulo 360 ; une valeur hors {0, 90, 180, 270} ou une boîte
  dégénérée rend la géométrie du DOCUMENT indisponible (``None``) — jamais une
  géométrie partielle ou corrigée ;
- ``page_label`` uniquement si le catalogue déclare ``/PageLabels`` ; sinon ``None``
  (pypdf fabriquerait sinon « 1, 2, 3… », ce qui serait une invention).
"""
from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class PdfPageGeometry:
    page_number: int
    mediabox: Tuple[float, float, float, float]
    cropbox: Tuple[float, float, float, float]
    rotate: int
    page_label: Optional[str] = None


class _InvalidGeometry(Exception):
    pass


def _number(v) -> float | int:
    n = float(v)
    if not math.isfinite(n):
        raise _InvalidGeometry("non-finite coordinate")
    return int(n) if n.is_integer() else n


def _box(rect) -> Tuple[float, float, float, float]:
    vals = [_number(v) for v in list(rect)]
    if len(vals) != 4:
        raise _InvalidGeometry("box must have 4 coordinates")
    x0, x1 = sorted((vals[0], vals[2]))
    y0, y1 = sorted((vals[1], vals[3]))
    if not (x1 > x0 and y1 > y0):
        raise _InvalidGeometry("degenerate box")
    return (x0, y0, x1, y1)


def read_pdf_page_geometry(data: bytes) -> Optional[List[PdfPageGeometry]]:
    """Géométrie de toutes les pages, ou ``None`` si elle n'est pas lisible sans ambiguïté."""
    try:
        from pypdf import PdfReader
        from pypdf.generic import NameObject
    except Exception:  # noqa: BLE001 - dépendance absente : pas de géométrie
        return None
    try:
        reader = PdfReader(io.BytesIO(data or b""))
        root = reader.trailer["/Root"]
        labels = reader.page_labels if NameObject("/PageLabels") in root else None
        out: List[PdfPageGeometry] = []
        for i, page in enumerate(reader.pages):
            mediabox = _box(page.mediabox)
            cropbox = _box(page.cropbox) if "/CropBox" in page else mediabox
            rotate = int(page.get("/Rotate", 0) or 0) % 360
            if rotate not in _ROTATIONS:
                raise _InvalidGeometry("invalid /Rotate")
            label = None
            if labels is not None and i < len(labels):
                label = str(labels[i]) or None
            out.append(PdfPageGeometry(page_number=i + 1, mediabox=mediabox, cropbox=cropbox,
                                       rotate=rotate, page_label=label))
        return out or None
    except Exception as e:  # noqa: BLE001 - la géométrie n'interrompt jamais l'extraction
        logger.debug("pdf page geometry unavailable: %s", type(e).__name__)
        return None
