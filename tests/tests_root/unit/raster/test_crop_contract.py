"""Contrat de crop ``ragatm-crop/2`` (CV-3C, tuile CV-3E1) — géométrie, pixels, encodage, tuilage."""
from __future__ import annotations

import hashlib
import io
import struct

import numpy as np
import pytest
from PIL import Image

from apps.rag_core.services.raster import crops as K


@pytest.fixture
def rgb():
    return np.random.default_rng(7).integers(0, 256, (100, 120, 3), dtype=np.uint8)


# -- bbox ------------------------------------------------------------------------------
def test_extraction_is_exact_and_half_open(rgb):
    px = K.extract_pixels(rgb, (10, 20, 13, 22))
    assert px.shape == (2, 3, 3)                        # [10,13) × [20,22)
    assert np.array_equal(px, rgb[20:22, 10:13, :]) and px.flags.c_contiguous
    full = K.extract_pixels(rgb, K.validate_bbox([0, 0, 120, 100], 120, 100))
    assert np.array_equal(full, rgb)


@pytest.mark.parametrize("bbox", [
    [-1, 0, 10, 10], [0, 0, 121, 10], [0, 0, 10, 101], [5, 5, 5, 10], [10, 5, 4, 10],
    [0, 0, 10.0, 10], [0, 0, True, 10], [0, 0, 10], "0,0,1,1",
])
def test_invalid_bbox_rejected_never_clamped(bbox):
    with pytest.raises(K.InvalidBBox):
        K.validate_bbox(bbox, 120, 100)


def test_padding_clamped_only_for_valid_bbox():
    assert K.pad_bbox((2, 3, 50, 60), 8, 120, 100) == (0, 0, 58, 68)
    assert K.pad_bbox((100, 90, 118, 99), 8, 120, 100) == (92, 82, 120, 100)
    with pytest.raises(K.InvalidBBox):
        K.pad_bbox((100, 90, 130, 99), 8, 120, 100)     # bbox hors raster : refusée
    with pytest.raises(K.InvalidBBox):
        K.pad_bbox((0, 0, 10, 10), -1, 120, 100)


# -- identité et encodage -----------------------------------------------------------
def test_artifact_hashes_follow_definitions(rgb):
    art = K.build_artifact(rgb, (4, 5, 40, 50), encode=True)
    px = rgb[5:50, 4:40, :]
    assert (art.width, art.height) == (36, 45)
    assert art.crop_pixels_sha256 == hashlib.sha256(struct.pack(">II", 36, 45) + px.tobytes()).hexdigest()
    assert art.crop_sha256 == hashlib.sha256(art.png).hexdigest()
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(art.png)).convert("RGB")), px)


def test_png_encoding_is_deterministic_and_metadata_free(rgb):
    pngs = {K.encode_png(rgb) for _ in range(5)}
    assert len(pngs) == 1
    (png,) = pngs
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert set(K.png_chunks(png)) == {"IHDR", "IDAT", "IEND"}
    assert Image.open(io.BytesIO(png)).mode == "RGB"
    assert K.ENCODING_ID == "png-rgb8-zl6/1"


def test_oversize_parent_is_never_encoded(rgb):
    art = K.build_artifact(rgb, (0, 0, 120, 100), encode=False)
    assert art.png is None and art.crop_sha256 is None and len(art.crop_pixels_sha256) == 64


# -- tuilage --------------------------------------------------------------------------
@pytest.mark.parametrize("bbox", [(0, 0, 3000, 2000), (17, 9, 1431, 5000), (0, 0, 1177, 1177),
                                  (100, 100, 4778, 6723)])
def test_tiles_cover_without_gap_within_2mp_and_page_relative(bbox):
    tiles = K.tile_bboxes(bbox)
    assert tiles == K.tile_bboxes(bbox)                         # déterministe
    assert tiles == sorted(tiles, key=lambda t: (t[1], t[0]))   # ligne par ligne
    x0, y0, x1, y1 = bbox
    cover = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    for tx0, ty0, tx1, ty1 in tiles:
        assert (tx1 - tx0) * (ty1 - ty0) <= K.MAX_SENDABLE_PIXELS
        assert x0 <= tx0 < tx1 <= x1 and y0 <= ty0 < ty1 <= y1   # repère du raster
        cover[ty0 - y0:ty1 - y0, tx0 - x0:tx1 - x0] = True
    assert cover.all()                                            # aucun trou


def test_tile_overlap_contract_exact():
    assert K.CROP_CONTRACT_VERSION == "ragatm-crop/2"
    assert (K.TILE_SIDE_PX, K.TILE_OVERLAP_PX) == (1176, 64)          # recouvrement CV-3C préservé
    assert K.MAX_SENDABLE_PIXELS == 1_382_976 == K.TILE_SIDE_PX ** 2 < K.MAX_CROP_PIXELS
    assert K.TILE_SIDE_PX - K.TILE_OVERLAP_PX == 1112                 # pas dérivé
    xs = sorted({t[0] for t in K.tile_bboxes((0, 0, 5000, 1000))})
    steps = [b - a for a, b in zip(xs, xs[1:])]
    assert steps[:-1] == [K.TILE_SIDE_PX - K.TILE_OVERLAP_PX] * (len(steps) - 1)
    assert xs[-1] + K.TILE_SIDE_PX == 5000                       # dernière alignée au bord
    assert K.TILE_SIDE_PX - steps[-1] >= K.TILE_OVERLAP_PX       # recouvrement ≥ contrat


def test_region_within_2mp_is_a_single_tile():
    assert K.tile_bboxes((0, 0, 1176, 1176)) == [(0, 0, 1176, 1176)]


# -- origine -------------------------------------------------------------------------
def test_origin_contract():
    ok = {"detector_id": "pdf-embedded-image/1", "detector_fingerprint": "", "reason": "embedded_image",
          "score": 0.9}
    assert K.validate_origin(ok) == ok
    for bad in ({"detector_id": "x", "reason": "r"},                     # fingerprint absent
                {"detector_id": "", "detector_fingerprint": "", "reason": "r"},
                {"detector_id": "x", "detector_fingerprint": "", "reason": " "},
                {"detector_id": "x", "detector_fingerprint": "", "reason": "r", "kind": "table"},
                {"detector_id": "x", "detector_fingerprint": "", "reason": "r", "score": True}):
        with pytest.raises(K.InvalidOrigin):
            K.validate_origin(bad)


# -- ragatm-crop/2 : borne d'encodage --------------------------------------------------
def test_incompressible_tile_fits_gateway_base64_limit():
    """Pire cas mesuré (bruit uniforme, incompressible) : la tuile 1176² reste sous la
    limite ``image.data`` Gateway ; la tuile crop/1 (1414²) la dépassait."""
    import base64

    from apps.llm_router.gateway.vision_v2 import IMAGE_DATA_MAX_CHARS

    rng = np.random.default_rng(20261007)
    tile = rng.integers(0, 256, (K.TILE_SIDE_PX, K.TILE_SIDE_PX, 3), dtype=np.uint8)
    assert len(base64.b64encode(K.encode_png(tile))) <= IMAGE_DATA_MAX_CHARS
    old = rng.integers(0, 256, (1414, 1414, 3), dtype=np.uint8)
    assert len(base64.b64encode(K.encode_png(old))) > IMAGE_DATA_MAX_CHARS
