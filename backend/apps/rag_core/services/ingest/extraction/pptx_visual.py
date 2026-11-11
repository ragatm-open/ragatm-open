# backend/apps/rag_core/services/ingest/extraction/pptx_visual.py
"""
Découverte des images INTÉGRÉES d'une présentation PPTX.

ADAPTATEUR DE SOURCE, et rien d'autre : il transforme les formes image que la traversée
de `pptx_extractor` a DÉJÀ rencontrées en ``VisualAsset``. Il n'appelle pas le Gateway,
ne normalise aucune réponse, ne publie rien, ne relit aucun texte. L'analyse appartient
au rail visuel générique déjà qualifié pour l'image autonome, le PDF et le DOCX.

## Aucune ouverture supplémentaire

python-pptx a déjà chargé en mémoire toutes les parties ATTEIGNABLES du paquet, bornées
par la garde anti zip-bomb de la pré-passe. Les octets d'une image se lisent donc sur la
partie déjà chargée (`Part.blob`) : aucune réouverture de l'archive, aucune seconde
traversée de la présentation. Le compteur `package_opens_additional` rend cet invariant
exécutable.

## Intégrées seulement

La chaîne admise est `p:pic` -> `a:blip/@r:embed` -> relation de la diapositive ->
partie sous `/ppt/media/`. Une image LIÉE (`r:link`) n'est jamais suivie : ni requête,
ni fichier local, ni partage réseau. Un média présent dans l'archive mais référencé par
aucune diapositive n'est jamais rencontré, puisque seule la traversée des formes alimente
cet adaptateur. Les formats vectoriels (SVG, EMF, WMF) sont omis avec un compteur : ce
lot n'introduit aucune chaîne vectorielle.

## Une diapositive n'est pas une page

``source_page`` reste ``None``. La provenance est STRUCTURELLE : numéro de diapositive
(1-based), rang de la forme, identifiant de relation (local à la diapositive), partie du
paquet, appartenance à un groupe.

## Dédoublonnage AVANT le contexte

Le rail générique dédoublonne par clé d'analyse, qui inclut le contexte. Le contexte
d'une image PPTX porte le titre de sa diapositive : la même image sur trois diapositives
aux titres différents produirait trois clés, donc trois appels. Le regroupement par
octets normalisés se fait donc ICI, avant la construction des ``VisualAsset`` : un
groupe = un visuel = au plus un appel, avec le contexte de sa première occurrence.
"""
from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import config

logger = logging.getLogger(__name__)

_MEDIA_PREFIX = "/ppt/media/"

#: Formats VECTORIELS : aucun décodage raster sûr n'existe dans ce dépôt pour eux.
_VECTOR_CONTENT_TYPES = frozenset({
    "image/svg+xml", "image/x-emf", "image/emf", "image/x-wmf", "image/wmf",
    "image/x-pict", "image/pict",
})
_VECTOR_EXTENSIONS = frozenset({"svg", "emf", "emz", "wmf", "wmz", "pict", "pct"})

_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class PptxVisualLimits:
    """Bornes génériques. RÉUTILISE les réglages visuels existants (préfixés
    `DOCUMENT_PDF_VISUAL_` pour des raisons historiques, comme pour le DOCX). Le plafond
    « par page » du PDF s'applique ici PAR DIAPOSITIVE : c'est la même borne d'unité
    structurelle, sans nouvelle variable."""

    max_assets: int = 12
    max_per_slide: int = 4
    min_side_px: int = 96
    min_pixels: int = 20_000
    max_pixels: int = 2_000_000
    max_bytes: int = 8_000_000
    max_repeats: int = 3
    max_occurrences: int = 64

    @classmethod
    def from_settings(cls) -> "PptxVisualLimits":
        return cls(
            max_assets=config.pdf_visual_max_assets(),
            max_per_slide=config.pdf_visual_max_assets_per_page(),
            min_side_px=config.pdf_visual_min_side_px(),
            min_pixels=config.pdf_visual_min_pixels(),
            max_pixels=config.pdf_visual_max_pixels(),
            max_bytes=config.pdf_visual_max_bytes(),
            max_repeats=config.pdf_visual_max_repeats(),
        )


@dataclass(frozen=True)
class PptxPicture:
    """Forme image rencontrée par la traversée native, avec son contexte structurel."""

    slide_number: int
    shape_order: int
    grouped: bool
    shape: Any = field(repr=False, compare=False)
    alt_text: str = ""
    slide_title: Optional[str] = None


@dataclass
class PptxVisualDiscovery:
    assets: List = field(default_factory=list)
    stats: Dict = field(default_factory=dict)


def _new_stats() -> Dict:
    return {
        "package_opens_additional": 0,
        "pictures_seen": 0,
        "occurrences": 0,
        "parts_decoded": 0,
        "admitted": 0,
        "omitted_external": 0,
        "omitted_unresolved": 0,
        "omitted_vector": 0,
        "omitted_unreadable": 0,
        "omitted_too_small": 0,
        "omitted_too_large": 0,
        "omitted_decorative": 0,
        "omitted_limit": 0,
    }


@dataclass
class _Occurrence:
    slide_number: int
    shape_order: int
    grouped: bool
    rel_id: str
    alt_text: str
    slide_title: Optional[str]


def _blip(shape):
    try:
        return shape._element.blipFill.blip
    except Exception:  # noqa: BLE001 - forme exotique : pas d'image exploitable
        return None


def _resolve(shape, stats: Dict):
    """``(rel_id, part)`` de l'image INTÉGRÉE, ou ``None`` (compteur incrémenté)."""
    blip = _blip(shape)
    rel_id = None
    if blip is not None:
        try:
            rel_id = blip.rEmbed
        except Exception:  # noqa: BLE001
            rel_id = None
    if not rel_id:
        # Aucune référence intégrée. Une référence LIÉE n'est jamais suivie.
        if blip is not None and blip.get(f"{_R_NS}link"):
            stats["omitted_external"] += 1
        else:
            stats["omitted_unresolved"] += 1
        return None
    try:
        rel = shape.part.rels[rel_id]
    except Exception:  # noqa: BLE001 - relation absente ou cassée
        stats["omitted_unresolved"] += 1
        return None
    if getattr(rel, "is_external", False):
        stats["omitted_external"] += 1
        return None
    try:
        part = rel.target_part
        partname = posixpath.normpath(str(part.partname))
    except Exception:  # noqa: BLE001
        stats["omitted_unresolved"] += 1
        return None
    if not partname.startswith(_MEDIA_PREFIX):
        stats["omitted_unresolved"] += 1
        return None
    return rel_id, part, partname


def _is_vector(part, partname: str) -> bool:
    content_type = str(getattr(part, "content_type", "") or "").lower()
    extension = partname.rsplit(".", 1)[-1].lower() if "." in partname else ""
    return content_type in _VECTOR_CONTENT_TYPES or extension in _VECTOR_EXTENSIONS


def discover_pptx_visual_assets(pictures: Sequence[PptxPicture], *,
                                limits: Optional[PptxVisualLimits] = None) -> PptxVisualDiscovery:
    """Visuels intégrés ADMIS d'une présentation, prêts pour le rail visuel générique.

    ``pictures`` : formes image relevées PENDANT la traversée existante, dans l'ordre de
    la présentation. Best-effort et SANS exception : une image hostile, cassée ou liée
    est omise avec un compteur ; l'ingestion ne doit jamais échouer à cause d'un visuel
    optionnel.
    """
    from ..visual.capability import visual_task_for_source
    from ..visual.contracts import (
        VisualAsset,
        VisualAssetError,
        VisualContext,
        VisualOccurrence,
        sha256_hex,
    )
    from .docx_visual import _decode, _fit_pixels
    from .image_extractor import normalize_rgb_png

    stats = _new_stats()
    out = PptxVisualDiscovery(assets=[], stats=stats)
    lim = limits or PptxVisualLimits.from_settings()

    # Décodage UNE fois par partie : python-pptx mutualise déjà les octets identiques en
    # une seule partie média, souvent référencée par des dizaines de diapositives.
    par_partie: Dict[str, Optional[str]] = {}
    par_hash: Dict[str, dict] = {}
    ordre: List[str] = []

    for pic in pictures:
        stats["pictures_seen"] += 1
        resolu = _resolve(pic.shape, stats)
        if resolu is None:
            continue
        rel_id, part, partname = resolu
        occurrence = _Occurrence(pic.slide_number, pic.shape_order, bool(pic.grouped),
                                 rel_id, pic.alt_text or "", pic.slide_title)

        if partname in par_partie:
            empreinte = par_partie[partname]
            if empreinte is not None:
                par_hash[empreinte]["occurrences"].append(occurrence)
                stats["occurrences"] += 1
            continue
        par_partie[partname] = None

        if _is_vector(part, partname):
            stats["omitted_vector"] += 1
            continue
        try:
            brut = part.blob
        except Exception:  # noqa: BLE001
            brut = b""
        if not brut:
            stats["omitted_unreadable"] += 1
            continue
        # Borne AVANT tout décodage : les octets sont déjà en mémoire, mais aucun
        # décodage PIL ne doit être tenté au-delà du plafond.
        if len(brut) > lim.max_bytes:
            stats["omitted_too_large"] += 1
            continue

        stats["parts_decoded"] += 1
        img = _decode(brut, max_pixels=lim.max_pixels)
        if img is None:
            stats["omitted_unreadable"] += 1
            continue
        if (min(img.width, img.height) < lim.min_side_px
                or (img.width * img.height) < lim.min_pixels):
            stats["omitted_too_small"] += 1
            continue
        try:
            normalise = normalize_rgb_png(_fit_pixels(img, lim.max_pixels))
        except Exception:  # noqa: BLE001 - un média hostile ne casse jamais l'ingestion
            normalise = None
        if normalise is None:
            stats["omitted_unreadable"] += 1
            continue
        contenu, largeur, hauteur = normalise
        if len(contenu) > lim.max_bytes:
            stats["omitted_too_large"] += 1
            continue

        empreinte = sha256_hex(contenu)
        par_partie[partname] = empreinte
        stats["occurrences"] += 1
        entree = par_hash.get(empreinte)
        if entree is None:
            entree = {"content": contenu, "width": largeur, "height": hauteur,
                      "part": partname.lstrip("/"), "occurrences": []}
            par_hash[empreinte] = entree
            ordre.append(empreinte)
        entree["occurrences"].append(occurrence)

    par_diapositive: Dict[int, int] = {}
    for empreinte in ordre:
        e = par_hash[empreinte]
        occurrences = e["occurrences"]

        # Octets identiques répétés : logo, bandeau, pictogramme. Signal GÉNÉRIQUE,
        # jamais un nom de fichier ni un texte de marque.
        if len(occurrences) > lim.max_repeats:
            stats["omitted_decorative"] += 1
            continue
        premiere = occurrences[0]
        if par_diapositive.get(premiere.slide_number, 0) >= lim.max_per_slide:
            stats["omitted_limit"] += 1
            continue
        if stats["admitted"] >= lim.max_assets:
            stats["omitted_limit"] += 1
            continue

        locator = {
            "type": "pptx",
            "slide_number": premiere.slide_number,
            "shape_order": premiere.shape_order,
            "rel_id": premiere.rel_id,
            "part": e["part"],
            "grouped": premiere.grouped,
        }
        diapositives = sorted({o.slide_number for o in occurrences})
        if len(occurrences) > 1:
            locator["slide_numbers"] = diapositives

        try:
            asset = VisualAsset(
                content=e["content"],
                mime_type="image/png",
                width=e["width"],
                height=e["height"],
                source_kind="pptx",
                content_hash=empreinte,
                ordinal=stats["admitted"],
                # Une diapositive n'est pas une page : aucun numéro n'est inventé.
                source_page=None,
                source_bbox=None,
                source_locator=locator,
                # Contexte d'AIDE au modèle, jamais une preuve : texte alternatif SAISI
                # par l'auteur et titre de la diapositive. Ni corps, ni notes.
                context=VisualContext(caption=None,
                                      alt_text=premiere.alt_text or None,
                                      nearby_text=premiere.slide_title or None),
                origin="embedded",
                occurrences=tuple(
                    VisualOccurrence(source_page=None, source_bbox=None)
                    for _ in occurrences[1:lim.max_occurrences]
                ),
                task=visual_task_for_source("pptx"),
            )
        except VisualAssetError as exc:
            logger.debug("pptx visual asset rejected: %s", exc)
            stats["omitted_unreadable"] += 1
            continue

        out.assets.append(asset)
        stats["admitted"] += 1
        par_diapositive[premiere.slide_number] = par_diapositive.get(premiere.slide_number, 0) + 1

    return out
