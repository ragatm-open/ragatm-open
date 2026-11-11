"""Projection région PDF → pixels du raster.

Deux familles de preuves :
  * F06–F09 (vecteurs CV-3A) : la CropBox entière se projette EXACTEMENT sur le raster
    entier, pour chaque rotation et chaque DPI mesuré ;
  * PDF synthétiques : une image unie posée à un rectangle connu, repérée comme le fait
    ``pdf_visual`` (``get_image_rects``), puis rendue par ``ragatm-pdfium/1`` — la bbox
    projetée coïncide avec les pixels de l'image à ``PROJECTION_TOLERANCE_PX`` près par bord,
    et la bbox avec marge de crop les CONTIENT tous.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from apps.rag_core.services.ingest.extraction.pdf_geometry import read_pdf_page_geometry
from apps.rag_core.services.raster import contract as C
from apps.rag_core.services.raster.crops import pad_bbox
from apps.rag_core.services.raster.geometry import (
    PROJECTION_TOLERANCE_PX,
    VISUAL_CROP_PADDING_PX,
    project_region_to_raster,
    rotate_rect_points,
)

VECTORS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "raster_vectors"


def _vectors():
    data = json.loads((VECTORS_DIR / "vectors.json").read_text())
    return [v for v in data["vectors"] if v["fixture"][:3] in {"F06", "F07", "F08", "F09"}]


@pytest.mark.parametrize("vec", _vectors(), ids=lambda v: f"{v['fixture'][:3]}-{v['dpi']}")
def test_full_cropbox_projects_onto_full_raster(vec):
    geom = read_pdf_page_geometry((VECTORS_DIR / vec["fixture"]).read_bytes())[0]
    w_pt = geom.cropbox[2] - geom.cropbox[0]
    h_pt = geom.cropbox[3] - geom.cropbox[1]
    bbox = project_region_to_raster((0, 0, w_pt, h_pt), cropbox=geom.cropbox, rotate=geom.rotate,
                                    dpi=vec["dpi"], raster_width=vec["width"],
                                    raster_height=vec["height"])
    assert bbox == (0, 0, vec["width"], vec["height"])
    assert C.expected_raster_size(geom.cropbox, geom.rotate, vec["dpi"]) == (vec["width"], vec["height"])


# -- formules pures ----------------------------------------------------------------------
@pytest.mark.parametrize("rotate,expected", [
    (0, (10, 20, 30, 50)),
    (90, (100 - 50, 10, 100 - 20, 30)),
    (180, (200 - 30, 100 - 50, 200 - 10, 100 - 20)),
    (270, (20, 200 - 30, 50, 200 - 10)),
])
def test_rotation_formulas(rotate, expected):
    assert rotate_rect_points((10, 20, 30, 50), width_pt=200, height_pt=100, rotate=rotate) == expected


def test_outward_rounding_and_clipping():
    cb = (0, 0, 72, 72)
    # 1 pt = 200/72 px : 0.5 pt → 1.388… px, plancher 1 ; 10.1 pt → 28.05… px, plafond 29
    assert project_region_to_raster((0.5, 0.5, 10.1, 10.1), cropbox=cb, rotate=0, dpi=200,
                                    raster_width=200, raster_height=200) == (1, 1, 29, 29)
    # débord de la CropBox : rogné au raster, jamais d'index négatif
    assert project_region_to_raster((-5, -5, 80, 80), cropbox=cb, rotate=0, dpi=200,
                                    raster_width=200, raster_height=200) == (0, 0, 200, 200)


@pytest.mark.parametrize("rect", [
    None, (1, 2, 3), (5, 5, 5, 10), (10, 5, 4, 10), (0, 0, math.nan, 10), (0, 0, math.inf, 10),
    ("a", 0, 1, 1), (200, 200, 300, 300),            # entièrement hors page
])
def test_invalid_or_empty_region_is_none(rect):
    assert project_region_to_raster(rect, cropbox=(0, 0, 72, 72), rotate=0, dpi=150,
                                    raster_width=150, raster_height=150) is None


def test_invalid_rotation_or_cropbox_is_none():
    assert project_region_to_raster((0, 0, 1, 1), cropbox=(0, 0, 72, 72), rotate=45, dpi=150,
                                    raster_width=150, raster_height=150) is None
    assert project_region_to_raster((0, 0, 1, 1), cropbox=(0, 0, 0, 72), rotate=0, dpi=150,
                                    raster_width=150, raster_height=150) is None


def test_projection_is_deterministic():
    args = dict(cropbox=(30, 40, 500, 700), rotate=270, dpi=200, raster_width=1834, raster_height=1306)
    first = project_region_to_raster((12.3456, 78.9, 210.0001, 333.3), **args)
    assert all(project_region_to_raster((12.3456, 78.9, 210.0001, 333.3), **args) == first
               for _ in range(50))


# -- preuve par le rendu -----------------------------------------------------------------
fitz = pytest.importorskip("fitz")


def _synthetic_pdf(rotate: int, cropbox):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    if cropbox is not None:
        page.set_cropbox(fitz.Rect(*cropbox))
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False)
    pix.set_rect(pix.irect, (220, 0, 0))
    # rectangle en coordonnées de page non pivotée (repère PyMuPDF, origine CropBox)
    page.insert_image(fitz.Rect(97.3, 151.7, 398.6, 402.2), pixmap=pix)
    page.set_rotation(rotate)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("cropbox", [None, (41, 63, 541, 803)], ids=["mediabox", "cropbox"])
def test_projection_matches_rendered_pixels(rotate, cropbox):
    data = _synthetic_pdf(rotate, cropbox)
    with fitz.open(stream=data, filetype="pdf") as doc:
        page = doc[0]
        xref = page.get_images(full=True)[0][0]
        src = tuple(round(float(v), 4) for v in page.get_image_rects(xref)[0])   # comme pdf_visual
    geom = read_pdf_page_geometry(data)[0]
    assert geom.rotate == rotate
    dpi = 150
    raster = C.render_pdf_page(data, page_number=1, cropbox=geom.cropbox, rotate=geom.rotate, dpi=dpi)
    rgb = np.asarray(raster.rgb)
    red = (rgb[:, :, 0] > 120) & (rgb[:, :, 1] < 120) & (rgb[:, :, 2] < 120)
    ys, xs = np.nonzero(red)
    seen = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    proj = project_region_to_raster(src, cropbox=geom.cropbox, rotate=geom.rotate, dpi=dpi,
                                    raster_width=raster.width, raster_height=raster.height)
    assert proj is not None
    # PDFium cale l'image sur la grille de pixels : écart d'au plus un pixel par bord …
    assert all(abs(a - b) <= PROJECTION_TOLERANCE_PX for a, b in zip(proj, seen)), (proj, seen)
    # … absorbé par la marge de crop : la région découpée CONTIENT tout le visuel rendu
    padded = pad_bbox(proj, VISUAL_CROP_PADDING_PX, raster.width, raster.height)
    assert padded[0] <= seen[0] and padded[1] <= seen[1] and padded[2] >= seen[2] and padded[3] >= seen[3]
