"""Contrat de crop ``ragatm-crop/2`` (CV-3C ; tuile 1176 px CV-3E1) : géométrie, pixels, encodage, tuilage.

Repère NORMATIF : pixels entiers du ``PageRaster`` rendu ; origine en haut à gauche,
x vers la droite, y vers le bas ; bbox ``[x_min, y_min, x_max, y_max]`` DEMI-OUVERTE
(``[x_min, x_max) × [y_min, y_max)``) ; ``0 ≤ x_min < x_max ≤ W``, idem en y. Aucune
bbox invalide n'est corrigée : seul le PADDING demandé est rogné au bord du raster.

Pixels : sous-rectangle EXACT du tampon RGB8 du raster — aucun redimensionnement,
aucune rotation, aucune interpolation, aucune conversion de couleur.

Encodage (``png-rgb8-zl6/1``) : PNG RGB 8 bits, ``compress_level=6``, sans métadonnée
(chunks IHDR/IDAT/IEND seulement). ``crop_sha256`` porte sur ces octets exacts, avant
tout Base64 ; ``crop_pixels_sha256`` sur les pixels (identité de reproduction).

Tuilage (``ragatm-crop/2``) : une région > ``MAX_SENDABLE_PIXELS`` n'est
JAMAIS redimensionnée. Elle reste un crop parent sans artefact, découpé en tuiles de côté
≤ ``TILE_SIDE_PX`` (1176) avec un recouvrement de ``TILE_OVERLAP_PX`` entre tuiles
voisines ; la dernière tuile de chaque axe est alignée sur le bord de la région
(recouvrement ≥ ``TILE_OVERLAP_PX``, jamais de
trou). Les bbox des tuiles sont exprimées dans le raster, pas dans le parent.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .contract import pixels_sha256, sha256_hex

CROP_CONTRACT_VERSION = "ragatm-crop/2"
ENCODING_ID = "png-rgb8-zl6/1"
#: Plafond PIXELS du contrat Gateway (2 MP) — limite dure, jamais la limite pratique.
MAX_CROP_PIXELS = 2_000_000
#: Limite pratique ENVOYABLE (ragatm-crop/2). Le plafond encodé de la Gateway
#: (``image.data`` ≤ 5 592 408 caractères Base64 ≈ 4 194 306 octets PNG) est atteint bien
#: avant 2 MP par un contenu photographique ou incompressible : mesuré (zlib-ng, niveau 6),
#: un PNG incompressible pèse +0,17 % de ses octets RGB bruts ; 1414² dépasse (8,0 M car.),
#: 1182² dépasse encore, 1180² passe de justesse. 1176² = 1 382 976 px ≈ 4 155 000 octets
#: PNG au pire : marge ≈ 0,8 %, quel que soit le contenu. Borne par les PIXELS, donc
#: indépendante de l'encodeur — la garde encodée reste en défense (``crop_encoded_oversize``).
TILE_SIDE_PX = 1176
MAX_SENDABLE_PIXELS = TILE_SIDE_PX * TILE_SIDE_PX
#: Recouvrement entre tuiles voisines. 64 px ≈ 8 mm à 200 dpi, plus que la hauteur
#: d'une ligne de texte 10 pt (≈ 28 px à 200 dpi, ≈ 42 px à 300 dpi) : une ligne coupée
#: par une frontière de tuile est entière dans l'une des deux. Surcoût ≈ 4,5 % par axe.
TILE_OVERLAP_PX = 64
#: Raison d'origine réservée aux tuiles d'un parent.
REASON_TILE = "tile"

BBox = Tuple[int, int, int, int]


class CropError(ValueError):
    code = "crop_invalid"

    def __init__(self, detail: str = ""):
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class InvalidBBox(CropError):
    code = "crop_bbox_invalid"


class InvalidOrigin(CropError):
    code = "crop_origin_invalid"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_bbox(bbox: Sequence, width: int, height: int) -> BBox:
    """bbox entière, non vide, contenue dans le raster — sinon ``InvalidBBox``."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(_is_int(v) for v in bbox):
        raise InvalidBBox("bbox must be 4 integers [x_min, y_min, x_max, y_max]")
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise InvalidBBox(f"bbox {list(bbox)} outside raster {width}x{height} or empty")
    return (x0, y0, x1, y1)


def pad_bbox(detected: BBox, padding_px: int, width: int, height: int) -> BBox:
    """``inference_bbox`` = bbox détectée VALIDE + padding, rogné au raster (seul rognage permis)."""
    if not _is_int(padding_px) or padding_px < 0:
        raise InvalidBBox("padding_px must be a non-negative integer")
    x0, y0, x1, y1 = validate_bbox(detected, width, height)
    return (max(0, x0 - padding_px), max(0, y0 - padding_px),
            min(width, x1 + padding_px), min(height, y1 + padding_px))


def validate_origin(origin: dict) -> dict:
    """``{detector_id, detector_fingerprint, reason, score?}`` — pourquoi le crop existe."""
    if not isinstance(origin, dict):
        raise InvalidOrigin("origin must be an object")
    allowed = {"detector_id", "detector_fingerprint", "reason", "score"}
    if set(origin) - allowed:
        raise InvalidOrigin(f"unexpected origin keys {sorted(set(origin) - allowed)}")
    for key in ("detector_id", "reason"):
        if not isinstance(origin.get(key), str) or not origin[key].strip() or len(origin[key]) > 64:
            raise InvalidOrigin(f"origin.{key} must be a non-empty string (≤ 64)")
    if not isinstance(origin.get("detector_fingerprint", None), str):
        raise InvalidOrigin("origin.detector_fingerprint must be a string")
    score = origin.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))
                              or not math.isfinite(score)):
        raise InvalidOrigin("origin.score must be a finite number")
    return dict(origin)


def _axis_starts(start: int, end: int) -> List[int]:
    length = end - start
    if length <= TILE_SIDE_PX:
        return [start]
    stride = TILE_SIDE_PX - TILE_OVERLAP_PX
    starts = list(range(start, end - TILE_SIDE_PX, stride))
    starts.append(end - TILE_SIDE_PX)
    return starts


def tile_bboxes(bbox: BBox) -> List[BBox]:
    """Tuiles ≤ ``MAX_SENDABLE_PIXELS`` couvrant ``bbox`` sans trou, ordre ligne par ligne."""
    x0, y0, x1, y1 = bbox
    xs, ys = _axis_starts(x0, x1), _axis_starts(y0, y1)
    tw, th = min(TILE_SIDE_PX, x1 - x0), min(TILE_SIDE_PX, y1 - y0)
    return [(x, y, x + tw, y + th) for y in ys for x in xs]


def extract_pixels(rgb, bbox: BBox):
    """Sous-rectangle EXACT, C-contigu (``rgb`` : ndarray (H, W, 3) uint8)."""
    import numpy as np

    x0, y0, x1, y1 = bbox
    return np.ascontiguousarray(rgb[y0:y1, x0:x1, :])


def encode_png(pixels) -> bytes:
    """PNG ``png-rgb8-zl6/1`` : RGB8, compress_level=6, aucune métadonnée."""
    from PIL import Image

    image = Image.fromarray(pixels)  # (H, W, 3) uint8 → RGB
    if image.mode != "RGB":
        raise CropError(f"pixels must be RGB8, got {image.mode}")
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=6, optimize=False)
    return buf.getvalue()


def png_chunks(data: bytes) -> List[str]:
    import struct

    chunks, i = [], 8
    while i < len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        chunks.append(data[i + 4:i + 8].decode("ascii"))
        i += 12 + length
    return chunks


@dataclass(frozen=True)
class CropArtifact:
    bbox: BBox
    width: int
    height: int
    crop_pixels_sha256: str
    #: Octets PNG et leur hash — ``None`` pour un parent > ``MAX_SENDABLE_PIXELS``.
    png: Optional[bytes]
    crop_sha256: Optional[str]


def build_artifact(rgb, bbox: BBox, *, encode: bool) -> CropArtifact:
    pixels = extract_pixels(rgb, bbox)
    h, w = pixels.shape[:2]
    png = encode_png(pixels) if encode else None
    return CropArtifact(bbox=bbox, width=w, height=h, crop_pixels_sha256=pixels_sha256(w, h, pixels),
                        png=png, crop_sha256=sha256_hex(png) if png is not None else None)
