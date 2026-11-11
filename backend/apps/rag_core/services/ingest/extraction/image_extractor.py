# backend/apps/rag_core/services/ingest/extraction/image_extractor.py
from __future__ import annotations

"""
Extracteur d'images autonomes (JPEG/PNG/TIFF/WEBP/…) — Lot 1.

Socle SÛR : ouverture défensive (verify + garde octets/pixels anti
decompression-bomb + correction d'orientation EXIF) puis OCR via le provider
EXISTANT (aucune duplication de la logique Tesseract).

⚠️ Lot 1 : le GATE fonctionnel reste inchangé — une image n'est acceptée que si
l'OCR global est activé (cf. validation.validate_and_classify). Le découplage
(`DOCUMENT_IMAGE_INGEST_ENABLED`, OCR image auto) et l'UX relèvent du Lot 2.

Import Pillow VOLONTAIREMENT paresseux (dans les fonctions) : son absence ne doit
pas casser l'import du registre d'extraction (qui sert aussi aux PDF/TXT/…).
"""

import logging
from io import BytesIO
from typing import Tuple

from . import config, mime as M
from .base import (
    Block,
    BlockType,
    ExtractionContext,
    ExtractionError,
    ExtractionResult,
)

logger = logging.getLogger(__name__)

# Tag EXIF "Orientation".
_EXIF_ORIENTATION = 0x0112


def safe_open_image(
    data: bytes,
    *,
    max_bytes: int,
    max_pixels: int,
    preprocess: bool = True,
) -> Tuple[object, dict]:
    """Ouvre une image de façon défensive et renvoie ``(image_PIL, info)``.

    Garanties :
    - refus si octets > ``max_bytes`` (``image_too_large``) ;
    - ``Image.MAX_IMAGE_PIXELS`` posé sur ``max_pixels`` (anti decompression-bomb) ;
    - ``verify()`` sur un flux frais (détection corruption) ;
    - réouverture après verify (verify() rend le handle inutilisable) ;
    - contrôle ``width*height <= max_pixels`` AVANT ``load()`` (``image_too_large``) ;
    - capture ``DecompressionBombError`` -> ``image_too_large`` ;
    - capture des erreurs Pillow -> ``image_corrupted`` ;
    - ``ImageOps.exif_transpose`` si ``preprocess`` (orientation EXIF appliquée).

    ``info`` : ``{format, width, height, mode, exif_orientation_applied}``.
    """
    data = data or b""
    if not data:
        raise ExtractionError("Image vide ou illisible.", code="image_corrupted")
    if max_bytes and len(data) > int(max_bytes):
        raise ExtractionError(
            f"Image trop volumineuse : {len(data)} octets (limite : {int(max_bytes)}).",
            code="image_too_large",
        )

    try:
        from PIL import Image, ImageOps  # import paresseux (extra [ocr])
    except Exception as e:  # pragma: no cover - dépend de l'env
        raise ExtractionError(
            "Moteur image indisponible (Pillow non installé). Installez l'extra OCR.",
            code="ocr_engine_missing",
        ) from e

    prev_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = int(max_pixels) if max_pixels else None
    try:
        # 1) verify() sur un flux frais (détecte la corruption ; consomme le handle).
        try:
            with Image.open(BytesIO(data)) as probe:
                probe.verify()
        except Image.DecompressionBombError as e:
            raise ExtractionError("Image trop volumineuse (pixels).", code="image_too_large") from e
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError("Image corrompue ou illisible.", code="image_corrupted") from e

        # 2) Réouverture pour usage réel.
        try:
            img = Image.open(BytesIO(data))
            fmt = (img.format or "").upper()
            width, height = img.size
            try:
                orientation = int(img.getexif().get(_EXIF_ORIENTATION, 1) or 1)
            except Exception:
                orientation = 1
        except Image.DecompressionBombError as e:
            raise ExtractionError("Image trop volumineuse (pixels).", code="image_too_large") from e
        except Exception as e:
            raise ExtractionError("Image corrompue ou illisible.", code="image_corrupted") from e

        # 3) Garde pixels AVANT décodage complet.
        if max_pixels and (int(width) * int(height)) > int(max_pixels):
            raise ExtractionError(
                f"Image trop volumineuse : {width}x{height} pixels (limite : {int(max_pixels)}).",
                code="image_too_large",
            )

        # 4) Pré-traitement + décodage (load) sous garde.
        exif_applied = False
        try:
            if preprocess and orientation not in (0, 1):
                img = ImageOps.exif_transpose(img)
                exif_applied = True
            img.load()
        except Image.DecompressionBombError as e:
            raise ExtractionError("Image trop volumineuse (pixels).", code="image_too_large") from e
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError("Image corrompue ou illisible.", code="image_corrupted") from e

        info = {
            "format": fmt,
            "width": int(img.width),
            "height": int(img.height),
            "mode": str(img.mode),
            "exif_orientation_applied": bool(exif_applied),
        }
        return img, info
    finally:
        Image.MAX_IMAGE_PIXELS = prev_max


def flatten_rgb(img):
    """Image RVB telle que la normalisation l'enverra : transparence composée sur BLANC.

    Partagée par ``normalize_rgb_png`` et par les mesures faites AVANT l'envoi (aplats
    PDF) : une seule composition, donc des mesures portant sur les pixels réellement
    envoyés.
    """
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        rgb = Image.new("RGB", rgba.size, (255, 255, 255))
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        return rgb
    return img if img.mode == "RGB" else img.convert("RGB")


def normalize_rgb_png(img) -> Tuple[bytes, int, int] | None:
    """Normalisation VISUELLE commune à toutes les sources (image autonome, PDF…).

    Transparence composée sur fond BLANC puis ré-encodage PNG RVB — ce qui retire au
    passage toute métadonnée d'origine (EXIF, GPS…) avant le moindre envoi. Rend
    ``(octets_png, largeur, hauteur)``, ou ``None`` si l'image n'est pas normalisable.

    UNE seule normalisation pour tout le rail visuel : deux variantes produiraient deux
    empreintes différentes pour la même image, donc deux identités de requête.
    """
    try:
        rgb = flatten_rgb(img)
        buf = BytesIO()
        rgb.save(buf, format="PNG")
        return buf.getvalue(), int(rgb.width), int(rgb.height)
    except Exception:
        logger.warning("visual asset normalization failed (mode=%s)", getattr(img, "mode", "?"))
        return None


def _standalone_visual_asset(data: bytes, img, res: ExtractionResult):
    """``VisualAsset`` de l'image autonome, tel qu'il sera ENVOYÉ au Gateway.

    Normalisation SÛRE, déterministe et indépendante de l'extension :
    - image déjà ouverte et bornée par ``safe_open_image`` (orientation EXIF appliquée) ;
    - première image seulement d'un fichier multi-images, avec avertissement ;
    - transparence composée sur fond BLANC, puis ré-encodage PNG RVB — ce qui retire
      aussi toutes les métadonnées d'origine (EXIF, GPS…) avant tout envoi.
    Aucun appel ici : l'extracteur DÉCOUVRE, le rail visuel ANALYSE. Rend ``None`` si
    l'image ne peut pas être normalisée (le rail visuel ne s'applique alors pas).
    """
    from PIL import Image

    from ..visual.capability import visual_task_for_source
    from ..visual.contracts import VisualAsset, sha256_hex
    from ..visual.rail import WARN_MULTIFRAME

    try:
        with Image.open(BytesIO(data)) as probe:
            frames = int(getattr(probe, "n_frames", 1) or 1)
    except Exception:
        frames = 1
    # Préflight du contrat vision V1 (VISUAL-ENRICHMENT-DEFERRED) : au-delà de la borne
    # de pixels du Gateway, l'image est RÉDUITE — même fonction, même borne que les
    # visuels intégrés d'un PDF, qui n'ont produit AUCUN 413 sur 215 appels quand les
    # images autonomes envoyées en pleine résolution en produisaient (6,5 Mpx -> 413).
    # Ratio conservé, jamais d'agrandissement, déterministe ; l'original stocké n'est
    # pas touché — seule la représentation ENVOYÉE est réduite.
    from . import config as _cfg
    from .pdf_visual import _fit_pixels

    img = _fit_pixels(img, int(_cfg.pdf_visual_max_pixels()))
    normalized = normalize_rgb_png(img)
    if normalized is None:
        return None
    content, width, height = normalized
    if frames > 1:
        res.add_warning(WARN_MULTIFRAME)
    return VisualAsset(
        content=content,
        mime_type="image/png",
        width=width,
        height=height,
        source_kind="image",
        content_hash=sha256_hex(content),
        ordinal=0,
        source_locator={"type": "image"},
        origin="embedded",
        # Tâche OPÉRATIONNELLE de la source, pas le défaut générique du contrat
        # seule `describe` est qualifiée côté Gateway.
        task=visual_task_for_source("image"),
    )


class ImageExtractor:
    """Image autonome : OCR requis (le gate de validation refuse déjà si OCR off).

    Pipeline : ouverture sûre -> ré-encodage PNG -> OCR (provider existant) ->
    ExtractionResult (1 page) avec métadonnées image enrichies.
    """

    name = "image"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime in M.IMAGE_MIMES

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        res = ExtractionResult()

        img, info = safe_open_image(
            data,
            max_bytes=config.image_max_bytes(),
            max_pixels=config.image_max_pixels(),
            preprocess=config.image_preprocess_enabled(),
        )

        # Ré-encodage PNG en mémoire pour réutiliser le provider OCR existant
        # (aucune duplication de la logique Tesseract).
        buf = BytesIO()
        try:
            img.save(buf, format="PNG")
        except Exception as e:
            raise ExtractionError("Image illisible pour l'OCR.", code="image_corrupted") from e
        png_bytes = buf.getvalue()
        # source visuelle reconnue (transitoire, jamais persisté) ;
        # la découverte d'un visuel n'a lieu que si le pipeline l'a demandée.
        res.visual_source_kind = "image"
        visual_active = "image" in (ctx.visual_sources or ())

        from .ocr import OcrDisabled, OcrUnavailable, ocr_image

        try:
            ocr_res = ocr_image(png_bytes, page_timeout_s=ctx.ocr_page_timeout_s)
        except (OcrUnavailable, OcrDisabled) as e:
            raise ExtractionError(
                getattr(e, "detail", "OCR indisponible pour l'image."),
                code=getattr(e, "code", "ocr_unavailable"),
            )

        for w in ocr_res.warnings:
            res.add_warning(w)
        res.partial = ocr_res.partial
        text = ocr_res.text
        rotation = ocr_res.pages[0].rotation if ocr_res.pages else 0

        # Aucun texte exploitable : distinguer "moteur OCR absent" (à installer)
        # de "image illisible / sans texte" — message clair côté image (≠ générique
        # empty_document). Les métadonnées image sont conservées sur l'échec.
        if not (text or "").strip():
            err_meta = {
                "type": "image",
                "ocr_used": False,
                "image": dict(info),
                "extraction_status": "failed",
            }
            _ENGINE_MISSING = {
                "ocr_engine_missing", "ocr_pdf_rasterizer_missing",
                "ocr_unavailable", "ocr_no_provider", "ocr_failed",
            }
            if any(w in _ENGINE_MISSING for w in ocr_res.warnings):
                raise ExtractionError(
                    "Moteur OCR indisponible sur le serveur (binaire tesseract / "
                    "bindings absents). Installez l'extra OCR.",
                    code="ocr_engine_missing",
                    meta=err_meta,
                )
            # Un budget de page expiré n'est PAS une image illisible : c'est un travail
            # interrompu. La voie PDF tranche déjà ainsi ; sans cette branche, la voie
            # image rapportait deux causes fausses — `visual_analysis_failed` quand le
            # rail visuel est actif (l'échec SECONDAIRE du rail masquait la cause
            # PREMIÈRE), et `ocr_no_text` quand il est éteint, ce qui est pire encore :
            # ce code est classé « fournissez un meilleur fichier source », c'est-à-dire
            # une impasse conseillée pour un fichier parfaitement sain.
            #
            # Placée AVANT le rail visuel, cette branche fixe la PRIORITÉ des causes :
            # ce qui a empêché d'obtenir le texte l'emporte sur ce qui a échoué ensuite.
            # On lit `timeout_pages`, la donnée structurée, plutôt que le texte d'un
            # warning : un contrat qui repose sur une chaîne de caractères se casse à la
            # première reformulation.
            if ocr_res.timeout_pages:
                from .ocr import OcrPageTimeout

                raise OcrPageTimeout(
                    "Le budget OCR par page a été dépassé et aucun texte exploitable "
                    "n'a pu être extrait de l'image. Le travail Tesseract a été "
                    "interrompu : l'image n'est pas nécessairement illisible.",
                    code="ocr_page_timeout",
                    meta={**err_meta, "ocr_timeout_pages": list(ocr_res.timeout_pages),
                          "visual_rail_active": bool(visual_active)},
                )

            # rail visuel actif => « aucun texte OCR » n'est plus
            # terminal ICI. Le rail visuel peut encore produire une évidence PRIMARY ;
            # sans elle, le candidat échouera (`visual_no_usable_evidence`/
            # `visual_analysis_failed`). Rail éteint : échec historique inchangé.
            if visual_active:
                asset = _standalone_visual_asset(data, img, res)
                if asset is not None:
                    res.visual_assets.append(asset)
                    res.metadata["type"] = "image"
                    res.metadata["ocr_used"] = False
                    res.metadata["ocr_no_text"] = True
                    res.metadata["requires_visual_check"] = False
                    image_meta = dict(info)
                    if rotation:
                        image_meta["rotation_applied"] = rotation
                    res.metadata["image"] = image_meta
                    res.metadata["pages"] = 1
                    return res.finalize(
                        fmt="image",
                        mime=ctx.declared_content_type or info.get("format") or "image",
                        method="image:ocr",
                    )
            raise ExtractionError(
                "L'OCR n'a extrait aucun texte exploitable de l'image "
                "(illisible, sans texte ou délai dépassé).",
                code="ocr_no_text",
                meta=err_meta,
            )

        res.text = text
        # `pages` (et pas seulement `blocks`) pour que le chunking page-centric
        # conserve page_number=1 et calcule les entités métier sur la page.
        res.pages = [(1, text)]
        res.blocks = [Block(type=BlockType.OCR_PAGE, text=text, page_number=1)]

        # P3-b : reconstruction de tableaux depuis l'OCR (opt-in, fragile).
        if config.table_ocr_enabled():
            try:
                from .ocr import ocr_words_for_image
                from .table_extractor import extract_ocr_tables_from_pages, table_to_markdown, table_to_meta

                page_words = ocr_words_for_image(png_bytes)
                for t in extract_ocr_tables_from_pages(page_words):
                    res.tables.append({"markdown": table_to_markdown(t), "meta": table_to_meta(t)})
            except Exception:
                pass

        # Métadonnées image enrichies (Lot 1 — internes ; exposition API = Lot 2).
        res.metadata["type"] = "image"
        res.metadata["ocr_used"] = bool(text)
        # La page 1 d'une image EST une page OCR. Sans cette déclaration, le rail paginé de
        # l'indexation posait `source_is_ocr=False` et la politique de preuve numérique
        # classait ce texte comme « texte natif » — donc probant —, contrairement à la règle
        # « OCR = jamais probant » (défaut préexistant, mis au jour lors de la qualification).
        if text:
            res.metadata["ocr_pages"] = [1]
        res.metadata["requires_visual_check"] = bool(text)
        image_meta = dict(info)
        if rotation:
            image_meta["rotation_applied"] = rotation
        res.metadata["image"] = image_meta
        conf = ocr_res.avg_confidence
        if conf is not None:
            res.metadata["ocr_confidence"] = conf
            if conf < 50:
                res.add_warning("ocr_low_confidence")
        # Même contrat que le PDF : dire si la confiance a été CALCULÉE, jamais l'inventer.
        res.metadata["ocr_confidence_computed"] = config.ocr_compute_confidence()
        res.metadata["pages"] = 1

        if visual_active:
            asset = _standalone_visual_asset(data, img, res)
            if asset is not None:
                res.visual_assets.append(asset)

        return res.finalize(
            fmt="image",
            mime=ctx.declared_content_type or info.get("format") or "image",
            method="image:ocr",
        )
