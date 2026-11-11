"""Contrat de rendu ``ragatm-pdfium/1`` (CV-3A → CV-3C).

Une page PDF → un tampon RGB8 dont l'identité de REPRODUCTION est ``pixels_sha256``
(modèle ID-B). Tout ce qui peut changer les pixels est soit un paramètre canonique
(``rasterizer_params``, haché en ``params_sha256``), soit une raison d'incrémenter
``RASTERIZER_ID`` ; les versions de bibliothèques ne sont que des diagnostics.

Règles mesurées par CV-3A et appliquées ici sans les supposer :
- pypdfium2 rend la CropBox et applique ``/Rotate`` lui-même : aucune rotation
  supplémentaire. Taille = ``ceil(dimension_pt_après_rotation × dpi/72)`` — vérifiée
  APRÈS rendu (``RasterContractMismatch``), jamais corrigée.
- ``/UserUnit`` ignoré (PDFium n'expose pas d'API pour le lire).
- annotations et formulaires NON dessinés, explicitement (les défauts de pypdfium2
  dessinent les annotations ; le chemin OCR historique, lui, est inchangé).
- le rendu d'une police substituée dépend de l'HISTORIQUE du processus (CV-3C : trois
  pixels différents pour F13 selon les rendus précédents) : chaque page est rendue dans
  un interpréteur NEUF, après un préchauffage synthétique fixe (``process-v1``).
- pic mémoire mesuré ≈ 6 octets/pixel (bitmap natif + tampon RGB) : le rendu est
  refusé AVANT allocation au-delà de ``MAX_RASTER_PIXELS`` — jamais réduit en silence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

RASTERIZER_ID = "ragatm-pdfium/1"
#: DPI supportés par ce contrat (la politique métier par défaut n'est pas figée ici).
SUPPORTED_DPIS = (150, 200, 300)
#: Plafond d'un rendu de page. 80 MP ≈ 460 Mio de pic mesuré (6,0 o/px) : couvre l'A1 à
#: 300 dpi (69,7 MP mesurés, 400 Mio) ; un A0 à 300 dpi (≈139 MP) est refusé.
MAX_RASTER_PIXELS = 80_000_000
#: Préchauffage : page synthétique à police substituée rendue en tête d'un interpréteur
#: NEUF, puis la page réelle seule (cf. ``pdfium_render_child``).
FONT_WARMUP_ID = "process-v1"
RENDER_ISOLATION = "fresh-interpreter-per-page"


class RasterError(Exception):
    code = "raster_error"

    def __init__(self, detail: str = ""):
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class RasterContractMismatch(RasterError):
    code = "raster_contract_mismatch"


class RasterTooLarge(RasterError):
    code = "raster_pixels_exceed_limit"


class UnsupportedRasterProfile(RasterError):
    code = "raster_profile_unsupported"


# -- canonicalisation ------------------------------------------------------------------
def canonical_json(value) -> bytes:
    """JSON canonique : clés triées, séparateurs compacts, UTF-8 sans échappement ASCII."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data) -> str:
    return hashlib.sha256(data).hexdigest()


def rasterizer_params(dpi: int) -> dict:
    """SEULE construction des paramètres canoniques du contrat (ne pas dupliquer)."""
    if dpi not in SUPPORTED_DPIS:
        raise UnsupportedRasterProfile(f"dpi={dpi} not in {SUPPORTED_DPIS}")
    return {
        "box": "cropbox",
        "page_rotation": "applied",
        "extra_rotation": 0,
        "dpi": dpi,
        "size_rule": "ceil(box_pt_after_rotation * dpi / 72)",
        "user_unit": "ignored",
        "color": "RGB8",
        "background": "FFFFFF",
        "draw_annots": False,
        "forms": False,
        "antialias": "pdfium_default",
        "font_warmup": FONT_WARMUP_ID,
        "render_isolation": RENDER_ISOLATION,
    }


def params_sha256(params: dict) -> str:
    return sha256_hex(canonical_json(params))


def pixels_sha256(width: int, height: int, rgb8) -> str:
    """SHA-256( uint32_be(W) ‖ uint32_be(H) ‖ RGB8 row-major haut→bas, gauche→droite ).

    ``rgb8`` : objet tampon C-contigu de exactement ``W×H×3`` octets (aucun stride).
    """
    view = memoryview(rgb8).cast("B")
    if view.nbytes != width * height * 3:
        raise RasterContractMismatch("rgb8 buffer size != width*height*3")
    h = hashlib.sha256(struct.pack(">II", width, height))
    h.update(view)
    return h.hexdigest()


def expected_raster_size(cropbox: Sequence[float], rotate: int, dpi: int) -> tuple[int, int]:
    """Taille contractuelle, même formule que pypdfium2 (``ceil(pt × dpi/72)``)."""
    scale = dpi / 72
    w_pt, h_pt = cropbox[2] - cropbox[0], cropbox[3] - cropbox[1]
    if rotate in (90, 270):
        w_pt, h_pt = h_pt, w_pt
    return math.ceil(w_pt * scale), math.ceil(h_pt * scale)


def check_raster_budget(width: int, height: int) -> None:
    if width * height > MAX_RASTER_PIXELS:
        raise RasterTooLarge(f"{width}x{height} > {MAX_RASTER_PIXELS} pixels")


def implementation_diagnostics() -> dict:
    """Versions des bibliothèques — audit et revue de montée, JAMAIS identité."""
    import PIL
    import pypdfium2
    from PIL import features

    diag = {
        "pypdfium2": pypdfium2.V_PYPDFIUM2,
        "pdfium": pypdfium2.V_LIBPDFIUM_FULL,
        "pdfium_build": pypdfium2.V_BUILDNAME,
        "pillow": PIL.__version__,
    }
    try:
        diag["zlib_ng"] = features.version("zlib_ng")
    except Exception:  # noqa: BLE001 - diagnostic facultatif
        pass
    return diag


# -- rendu (interpréteur neuf par page) ------------------------------------------------
#: Budget d'un rendu de page (interpréteur + préchauffage + rendu) ; à l'expiration le
#: groupe de processus est tué — rien n'est abandonné en arrière-plan.
RENDER_TIMEOUT_S = 120
_CHILD = Path(__file__).with_name("pdfium_render_child.py")
_CHILD_ERRORS = {3: RasterContractMismatch}


@dataclass
class RenderedRaster:
    width: int
    height: int
    #: ``numpy.ndarray`` (H, W, 3) uint8 C-contigu — le tampon RGB8 normatif.
    rgb: object = field(repr=False)
    pixels_sha256: str
    rasterizer_id: str
    params: dict
    params_sha256: str
    implementation: dict


def _run_render_child(pdf_bytes: bytes, *, page_number: int, dpi: int, width: int, height: int,
                      warmup: bool = True, timeout_s: Optional[float] = None):
    """Lance ``pdfium_render_child`` dans un interpréteur NEUF et lit son RGB8.

    Le tampon est lu directement dans le tableau final (pas de copie intermédiaire) :
    pic ≈ 3 o/px ici + ≈ 3 o/px dans l'enfant (bitmap), mesuré.
    """
    import numpy as np

    argv = [sys.executable, "-I", str(_CHILD), str(dpi), str(page_number), str(width), str(height)]
    if not warmup:
        argv.append("--no-warmup")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    timer = threading.Timer(timeout_s or RENDER_TIMEOUT_S, _kill_group, args=(proc,))
    timer.start()
    try:
        try:
            proc.stdin.write(pdf_bytes)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        header = proc.stdout.read(8)
        rgb = None
        if len(header) == 8:
            w, h = struct.unpack(">II", header)
            if (w, h) != (width, height):
                raise RasterContractMismatch(f"child returned {w}x{h} != {width}x{height}")
            rgb = np.empty((height, width, 3), dtype=np.uint8)
            view, got = memoryview(rgb).cast("B"), 0
            while got < view.nbytes:
                n = proc.stdout.readinto(view[got:])
                if not n:
                    break
                got += n
            if got != view.nbytes:
                rgb = None
        err = proc.stderr.read().decode("utf-8", "replace").strip()[:120]
        code = proc.wait()
    finally:
        timer.cancel()
    if code != 0 or rgb is None:
        if code in (-signal.SIGKILL, -signal.SIGTERM):
            raise RasterError(f"render timeout or kill ({code})")
        raise _CHILD_ERRORS.get(code, RasterError)(err or f"render child exit {code}")
    return rgb


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def render_pdf_page(pdf_bytes: bytes, *, page_number: int, cropbox: Sequence[float], rotate: int,
                    dpi: int, timeout_s: Optional[float] = None) -> RenderedRaster:
    """Rend la page ``page_number`` (1-based) selon ``ragatm-pdfium/1``.

    ``cropbox``/``rotate`` : géométrie DÉCLARÉE de la ``Page`` (CV-3B), qui fixe la taille
    attendue AVANT toute allocation (plafond) puis la vérifie après rendu (garde, dans
    l'enfant puis ici).
    """
    params = rasterizer_params(dpi)
    exp_w, exp_h = expected_raster_size(cropbox, rotate, dpi)
    check_raster_budget(exp_w, exp_h)
    rgb = _run_render_child(pdf_bytes, page_number=page_number, dpi=dpi, width=exp_w, height=exp_h,
                            timeout_s=timeout_s)
    return RenderedRaster(
        width=exp_w, height=exp_h, rgb=rgb, pixels_sha256=pixels_sha256(exp_w, exp_h, rgb),
        rasterizer_id=RASTERIZER_ID, params=params, params_sha256=params_sha256(params),
        implementation=implementation_diagnostics(),
    )
