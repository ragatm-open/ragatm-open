"""Matérialisation ``PageRaster`` / ``Crop`` et stockage IMMUABLE des crops (CV-3C).

Ordre (aucune transaction ouverte pendant le rendu ni l'écriture de fichier) :

    1. octets source relus et vérifiés (SHA-256 == ``DocumentVersion.content_sha256``)
    2. rendu dans un interpréteur neuf (``contract.render_pdf_page``)
    3. pixels → PNG → hashes, en mémoire
    4. fichier écrit (nom = ``crop_sha256``, jamais écrasé)
    5. ligne(s) en base dans une transaction COURTE, verrous RagSpace puis Document

Si l'étape 5 échoue, le fichier de l'étape 4 reste un orphelin IMMUABLE que
``media_storage_audit`` signale (``orphan_file_detected``) et qu'une purge peut retirer.

Arborescence (dans le stockage Django, jamais un chemin machine) :
``rag_docs/<space>/<doc>/vision/<version_id>/crops/<crop_sha256>.png`` — adressage par
contenu LIMITÉ au document/à la version : aucune déduplication entre documents.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction

from apps.rag_core.models import Crop, DocumentVersion, Page, PageRaster
from apps.rag_core.services.documents.provenance import _lock_document

from . import contract
from .crops import (
    CROP_CONTRACT_VERSION,
    ENCODING_ID,
    MAX_SENDABLE_PIXELS,
    REASON_TILE,
    TILE_OVERLAP_PX,
    TILE_SIDE_PX,
    InvalidBBox,
    InvalidOrigin,
    build_artifact,
    pad_bbox,
    tile_bboxes,
    validate_bbox,
    validate_origin,
)

logger = logging.getLogger(__name__)

#: Origine des tuiles : le tuileur déterministe de ``ragatm-crop/2``.
TILER_ORIGIN = {
    "detector_id": "ragatm-tiler",
    "detector_fingerprint": f"{CROP_CONTRACT_VERSION};side={TILE_SIDE_PX};overlap={TILE_OVERLAP_PX}",
    "reason": REASON_TILE,
}


class ProvenanceIntegrityError(Exception):
    code = "provenance_integrity_error"

    def __init__(self, detail: str = ""):
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class SourceIntegrityFailure(ProvenanceIntegrityError):
    code = "raster_source_integrity_failure"


class StorageIntegrityFailure(ProvenanceIntegrityError):
    code = "storage_integrity_failure"


class RasterIdentityMismatch(ProvenanceIntegrityError):
    code = "raster_pixels_diverged"


class CropIdentityMismatch(ProvenanceIntegrityError):
    code = "crop_identity_diverged"


class DocumentGone(ProvenanceIntegrityError):
    code = "document_gone"


# -- stockage -----------------------------------------------------------------------
def crop_storage_key(version: DocumentVersion, crop_sha256: str) -> str:
    doc = version.document
    return (f"rag_docs/{doc.rag_space_id}/{doc.id}/vision/{version.id}/crops/"
            f"{crop_sha256}.png")


def _stored_sha256(key: str) -> str:
    h = hashlib.sha256()
    with default_storage.open(key, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_immutable(key: str, data: bytes, sha256: str) -> None:
    """Écrit ``data`` sous ``key`` UNE fois. Fichier présent : même hash → no-op,
    hash différent → ``StorageIntegrityFailure``. Jamais d'écrasement."""
    if default_storage.exists(key):
        if _stored_sha256(key) != sha256:
            raise StorageIntegrityFailure("stored crop bytes differ from their content address")
        return
    saved = default_storage.save(key, ContentFile(data))
    if saved != key:
        # Course : un autre écrivain a créé `key` entre-temps ; le stockage a renommé.
        default_storage.delete(saved)
        if _stored_sha256(key) != sha256:
            raise StorageIntegrityFailure("concurrent write produced different bytes")


def read_version_bytes(version: DocumentVersion) -> bytes:
    with default_storage.open(version.storage_key, "rb") as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest() != version.content_sha256:
        raise SourceIntegrityFailure("source bytes differ from DocumentVersion.content_sha256")
    return data


def vision_storage_keys(**document_filter) -> List[str]:
    """Clés des fichiers de crop des documents filtrés (suppression, audit)."""
    filt = {f"page_raster__page__document_version__document__{k}": v
            for k, v in document_filter.items()}
    return sorted(set(Crop.objects.filter(**filt).exclude(storage_key__isnull=True)
                      .values_list("storage_key", flat=True)))


# -- PageRaster ----------------------------------------------------------------------
@dataclass
class RasterHandle:
    page_raster: PageRaster
    rendered: contract.RenderedRaster


def ensure_page_raster(page: Page, *, dpi: int, pdf_bytes: Optional[bytes] = None) -> RasterHandle:
    """Variante ``(page, ragatm-pdfium/1, params)`` — créée, ou réutilisée APRÈS avoir
    vérifié que le nouveau rendu donne exactement les mêmes pixels."""
    version = page.document_version
    data = pdf_bytes if pdf_bytes is not None else read_version_bytes(version)
    if pdf_bytes is not None and hashlib.sha256(data).hexdigest() != version.content_sha256:
        raise SourceIntegrityFailure("given bytes differ from DocumentVersion.content_sha256")
    rendered = contract.render_pdf_page(data, page_number=page.page_number, cropbox=page.cropbox,
                                        rotate=page.rotate, dpi=dpi)
    key = dict(page=page, rasterizer_id=rendered.rasterizer_id, params_sha256=rendered.params_sha256)
    with transaction.atomic():
        if _lock_document(version.document_id) is None:
            raise DocumentGone("document deleted during rasterisation")
        existing = PageRaster.objects.filter(**key).first()
        if existing is None:
            try:
                with transaction.atomic():
                    existing = PageRaster.objects.create(
                        **key, rasterizer_params=rendered.params, width=rendered.width,
                        height=rendered.height, pixels_sha256=rendered.pixels_sha256,
                        implementation=rendered.implementation)
            except IntegrityError:
                existing = PageRaster.objects.filter(**key).first()
                if existing is None:
                    raise
    if (existing.width, existing.height, existing.pixels_sha256) != (
            rendered.width, rendered.height, rendered.pixels_sha256):
        raise RasterIdentityMismatch("same raster contract rendered different pixels")
    return RasterHandle(page_raster=existing, rendered=rendered)


# -- Crop ------------------------------------------------------------------------
def _crop_row(raster: PageRaster, art, *, origin: dict, parent=None, detected_bbox=None,
              padding_px: int = 0, storage_key: Optional[str] = None) -> Crop:
    x0, y0, x1, y1 = art.bbox
    crop = Crop(page_raster=raster, parent_crop=parent, contract_version=CROP_CONTRACT_VERSION,
                x_min=x0, y_min=y0, x_max=x1, y_max=y1,
                detected_bbox=list(detected_bbox) if detected_bbox is not None else None,
                padding_px=padding_px, origin=origin, origin_reason=origin["reason"],
                width=art.width, height=art.height, crop_pixels_sha256=art.crop_pixels_sha256,
                crop_sha256=art.crop_sha256, encoding_id=ENCODING_ID if art.png is not None else None,
                storage_key=storage_key)
    return crop


def _get_or_create_crop(candidate: Crop) -> Crop:
    ident = dict(page_raster=candidate.page_raster, x_min=candidate.x_min, y_min=candidate.y_min,
                 x_max=candidate.x_max, y_max=candidate.y_max,
                 contract_version=candidate.contract_version, origin_reason=candidate.origin_reason)
    existing = Crop.objects.filter(**ident).first()
    if existing is None:
        try:
            with transaction.atomic():
                candidate.save()
                return candidate
        except IntegrityError:
            existing = Crop.objects.filter(**ident).first()
            if existing is None:
                raise
    same = (existing.crop_pixels_sha256, existing.crop_sha256, existing.storage_key,
            existing.parent_crop_id) == (candidate.crop_pixels_sha256, candidate.crop_sha256,
                                         candidate.storage_key, candidate.parent_crop_id)
    if not same:
        raise CropIdentityMismatch("existing crop with the same identity has other content")
    return existing


def create_crop(handle: RasterHandle, *, origin: dict, bbox: Optional[Sequence[int]] = None,
                detected_bbox: Optional[Sequence[int]] = None, padding_px: int = 0) -> Crop:
    """Crop ``ragatm-crop/2`` d'un raster rendu — idempotent.

    - ``bbox`` : bbox finale exacte (sans padding), OU ``detected_bbox`` + ``padding_px``
      (la bbox finale = détectée + padding rogné au raster).
    - ≤ ``MAX_SENDABLE_PIXELS`` : crop encodé (PNG écrit puis ligne). Au-delà : parent SANS artefact + tuiles
      encodées (``crop.tiles``), jamais de redimensionnement.
    """
    raster, rendered = handle.page_raster, handle.rendered
    origin = validate_origin(origin)
    if origin["reason"] == REASON_TILE:
        raise InvalidOrigin("reason 'tile' is reserved for generated tiles")
    if (bbox is None) == (detected_bbox is None):
        raise InvalidBBox("give exactly one of bbox or detected_bbox")
    if detected_bbox is not None:
        detected = validate_bbox(detected_bbox, raster.width, raster.height)
        final = pad_bbox(detected, padding_px, raster.width, raster.height)
    else:
        if padding_px:
            raise InvalidBBox("padding_px requires detected_bbox")
        detected, final = None, validate_bbox(bbox, raster.width, raster.height)

    w, h = final[2] - final[0], final[3] - final[1]
    oversize = w * h > MAX_SENDABLE_PIXELS
    main_art = build_artifact(rendered.rgb, final, encode=not oversize)
    tile_arts = [build_artifact(rendered.rgb, tb, encode=True) for tb in tile_bboxes(final)] if oversize else []

    version = raster.page.document_version
    keys = {}
    for art in [main_art, *tile_arts]:  # fichiers AVANT la base (étape 4)
        if art.png is not None:
            keys[art.bbox] = crop_storage_key(version, art.crop_sha256)
            write_immutable(keys[art.bbox], art.png, art.crop_sha256)

    with transaction.atomic():
        if _lock_document(version.document_id) is None:
            raise DocumentGone("document deleted during crop creation")
        parent = _get_or_create_crop(_crop_row(raster, main_art, origin=origin, detected_bbox=detected,
                                               padding_px=padding_px, storage_key=keys.get(main_art.bbox)))
        for art in tile_arts:
            _get_or_create_crop(_crop_row(raster, art, origin=dict(TILER_ORIGIN), parent=parent,
                                          storage_key=keys[art.bbox]))
    return parent
