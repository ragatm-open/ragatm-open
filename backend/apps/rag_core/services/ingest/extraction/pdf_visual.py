# backend/apps/rag_core/services/ingest/extraction/pdf_visual.py
"""
Découverte des visuels INTÉGRÉS d'un PDF.

Ce module est un ADAPTATEUR DE SOURCE, et rien d'autre : il transforme un PDF en
``VisualAsset``. Il n'appelle pas le Gateway, ne normalise aucune réponse, ne publie
rien. L'analyse appartient au rail visuel générique déjà qualifié pour l'image
autonome ; le PDF s'y branche sans architecture parallèle.

## Une seule ouverture, une seule itération, aucun rendu

La traversée PDF existante n'expose AUCUN handle réutilisable dans le cas courant :
``select_best_native_pdf_extraction`` s'arrête à pypdf dès que le texte natif suffit,
et pypdf n'expose ni bbox ni xref (mesuré : ses seuls attributs d'image sont
``data``/``name``/``indirect_reference``). Sans bbox, la non-duplication avec les
tableaux natifs et la sémantique de bbox persistée deviennent inapplicables.

Ce module ouvre donc le document UNE fois avec PyMuPDF et itère les pages UNE fois,
pour lire exclusivement ce qui est déjà présent : page, bbox, xref, dimensions,
octets. Exception délibérée et bornée :

* aucun rendu de page (``get_pixmap`` n'est jamais appelé) ;
* un raster doté d'un ``/SMask`` est décodé PAR SON XREF (``Pixmap(doc, xref)``), puis
  recomposé avec son masque — c'est un décodage d'image, pas un rendu de page ;
* aucun appel vision pendant la découverte ;
* aucune réouverture par page ni par image ;
* aucune duplication de l'extraction texte/OCR/tableaux.

Le compteur ``stats["pdf_opens"]`` / ``stats["pages_scanned"]`` rend cet invariant
EXÉCUTABLE : un test le vérifie, il n'est pas seulement documenté.

## Masques doux et aplats

``extract_image`` rend les octets de l'image DE BASE, sans son ``/SMask`` : une ombre
portée (aplat noir dont toute la forme vit dans le masque) sortait en rectangle noir
opaque, et une photo détourée sortait sur fond noir. Une image à ``/SMask`` est donc
désormais recomposée (base + masque) AVANT toute mesure, puis composée sur blanc par la
normalisation commune. Son ``content_hash`` change : c'est la correction voulue, l'ancienne
empreinte portait des pixels jamais affichés.

Une image SANS masque garde STRICTEMENT le chemin historique (``extract_image`` + PIL) :
le décodeur JPEG de MuPDF ne rend pas les mêmes pixels que celui de PIL, et remplacer
l'un par l'autre changerait l'identité de presque toutes les photos.

Un raster UNIFORME une fois recomposé (écart-type de luminance quasi nul sur l'image
entière ou sur sa zone centrale) ne porte aucune information et n'est plus envoyé
(``omitted_uniform``). Le filtre s'applique APRÈS la recomposition, jamais avant : une
silhouette dont tout le contenu est dans le masque paraîtrait noire sans elle.

## Sélection sur tout le document

Deux passes. La PASSE 1 applique les filtres puis mesure chaque candidat (surface affichée,
résolution, bandeau, répétition, dHash, vignette 32×32) en ne gardant AUCUN pixel. Si le
vivier tient dans le budget (``..._MAX_ASSETS``), tout est gardé dans l'ordre de
découverte, exactement comme avant. Sinon, les quasi-doublons perceptuels sont regroupés
(un représentant, aucune provenance ajoutée) puis S4-prop répartit le budget : une place
par segment de pages non vide (4 segments), le reste proportionnellement à la masse de
score, avec une pénalité de diversité par page. La PASSE 2 ne normalise QUE les candidats
retenus, dans l'ordre documentaire : ``ordinal`` reste un rang d'admission, jamais un
rang de score. Le budget Gateway ne change pas.

## Régions vectorielles rendues (opt-in)

Seule exception au « aucun rendu » : avec ``vector_regions=True`` (drapeau
``DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED`` ET espace listé), ``pdf_vector`` repère les régions
de tracés du MÊME document ouvert, et chacune est rendue SEULE (``get_pixmap(clip=…)``, jamais la
page entière), à ``DOCUMENT_PDF_VECTOR_REGION_RENDER_DPI`` réduit sous le plafond de pixels. Les
régions entrent dans le MÊME vivier que les images intégrées : mêmes filtres (taille, aplat,
tableau natif), même déduplication perceptuelle, même sélection S4-prop, même budget
``..._MAX_ASSETS`` — aucun appel Gateway supplémentaire par document. Une image intégrée contenue
dans une région retenue est remplacée par le rendu composite (``vector_raster_superseded``).
``VisualAsset`` : ``origin="rendered"``, ``render_dpi``, ``source_bbox`` dans le repère des images
intégrées (page non tournée, relatif à la CropBox), ``source_locator.region = "vector"``.
Paramètre faux : AUCUN code de ce chemin n'est exécuté (sortie identique au lot précédent).
"""
from __future__ import annotations

import dataclasses

import hashlib
import logging
import math
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Optional, Sequence, Tuple

from . import config

logger = logging.getLogger(__name__)

Bbox = Tuple[float, float, float, float]


@dataclass(frozen=True)
class PdfVisualLimits:
    """Bornes génériques, indépendantes de tout corpus."""

    max_assets: int = 12
    max_per_page: int = 4
    min_side_px: int = 96
    min_pixels: int = 20_000
    max_pixels: int = 2_000_000
    max_bytes: int = 8_000_000
    table_overlap_ratio: float = 0.6
    max_repeats: int = 3

    @classmethod
    def for_cap(cls, max_assets: Optional[int],
                max_per_page: Optional[int] = None) -> "PdfVisualLimits":
        """Bornes configurées, avec SEULEMENT le budget de visuels (document et/ou page)
        remplacé si donné. La borne par page agit pendant S4-prop et à l'admission."""
        limits = cls.from_settings()
        if max_assets is not None:
            limits = dataclasses.replace(limits, max_assets=int(max_assets))
        if max_per_page is not None:
            limits = dataclasses.replace(limits, max_per_page=int(max_per_page))
        return limits

    @classmethod
    def from_settings(cls) -> "PdfVisualLimits":
        return cls(
            max_assets=config.pdf_visual_max_assets(),
            max_per_page=config.pdf_visual_max_assets_per_page(),
            min_side_px=config.pdf_visual_min_side_px(),
            min_pixels=config.pdf_visual_min_pixels(),
            max_pixels=config.pdf_visual_max_pixels(),
            max_bytes=config.pdf_visual_max_bytes(),
            table_overlap_ratio=config.pdf_visual_table_overlap_ratio(),
            max_repeats=config.pdf_visual_max_repeats(),
        )


@dataclass
class PdfVisualDiscovery:
    assets: List = field(default_factory=list)
    stats: Dict = field(default_factory=dict)


def _new_stats() -> Dict:
    return {
        "pdf_opens": 0,
        "pages_scanned": 0,
        "images_seen": 0,
        "admitted": 0,
        "omitted_too_small": 0,
        "omitted_unreadable": 0,
        "omitted_too_large": 0,
        "omitted_table_overlap": 0,
        "omitted_decorative": 0,
        "omitted_limit": 0,
        "omitted_uniform": 0,
        "omitted_visual_duplicate": 0,
        "candidates_eligible": 0,
    }


def _overlap_ratio(inner: Bbox, outer: Bbox) -> float:
    """Part de ``inner`` couverte par ``outer``. Géométrie pure, aucun contenu lu."""
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    w = min(ix1, ox1) - max(ix0, ox0)
    h = min(iy1, oy1) - max(iy0, oy0)
    if w <= 0 or h <= 0:
        return 0.0
    area = (ix1 - ix0) * (iy1 - iy0)
    return (w * h) / area if area > 0 else 0.0


def _covers_native_table(bbox: Optional[Bbox], regions: Sequence[Bbox], ratio: float) -> bool:
    """Le visuel recouvre-t-il une région tabulaire DÉJÀ extraite nativement ?

    L'évidence native porte les relations ligne/colonne ; une description visuelle du
    même rectangle serait un doublon appauvri qui concurrencerait la représentation
    sûre au moment de la recherche.
    """
    if bbox is None or not regions:
        return False
    return any(_overlap_ratio(bbox, region) >= ratio for region in regions)


def _fit_pixels(img, max_pixels: int):
    """Réduit DÉTERMINISTEMENT une image au-dessus du plafond de pixels du Gateway.

    Réduire vaut mieux qu'écarter : le contrat V1 borne l'image à 2 Mpx, et un visuel
    utile ne doit pas être perdu pour un simple excès de définition. Le facteur ne
    dépend que des dimensions : la même image rend les mêmes octets, donc la même
    empreinte et la même identité de requête.
    """
    from PIL import Image

    w, h = int(img.width), int(img.height)
    if max_pixels <= 0 or (w * h) <= max_pixels:
        return img
    # Facteur entier de sous-échantillonnage : stable, sans interpolation dépendante
    # de la plateforme au-delà du filtre choisi.
    scale = (max_pixels / float(w * h)) ** 0.5
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    return img.resize((nw, nh), Image.LANCZOS)


def _decode(raw: bytes, *, max_pixels: int):
    """Décodage DÉFENSIF d'un raster intégré (bombe de décompression, format exotique)."""
    from PIL import Image

    prev = Image.MAX_IMAGE_PIXELS
    # Marge volontaire : on veut pouvoir DÉCODER une image un peu trop grande pour la
    # réduire ensuite, sans pour autant ouvrir la porte à une bombe.
    Image.MAX_IMAGE_PIXELS = max(int(max_pixels) * 8, 1)
    try:
        img = Image.open(BytesIO(raw))
        img.load()
        return img
    except Exception:
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = prev


#: Aplat (MASK-1B) : écart-type de luminance sous lequel une zone est réputée unie.
UNIFORM_MAX_STDDEV = 2.0
#: Zone CENTRALE mesurée en plus de l'image entière : retrait de 1/8 de chaque côté.
UNIFORM_CORE_INSET = 8


def _is_uniform(img) -> bool:
    """L'image AFFICHÉE (déjà recomposée) est-elle un aplat sans information ?

    Mesure déterministe et bon marché, sur la composition sur blanc que la normalisation
    enverrait : l'écart-type de luminance de l'image entière, puis celui de sa zone
    centrale. Une ombre portée est unie en son centre et ne varie qu'à ses bords doux ;
    un plan, même clairsemé, garde des traits contrastés qui font monter l'écart-type.

    La règle « couleur dominante ≥ 0,80 et au plus 64 couleurs » envisagée par
 n'est PAS retenue : sur un échantillon réduit, les traits fins
    d'un plan clairsemé s'estompent en quelques gris et le plan passait pour un aplat.
    Aucune classification, aucun vocabulaire, aucune marque.
    """
    from PIL import ImageStat

    from .image_extractor import flatten_rgb

    gray = flatten_rgb(img).convert("L")
    if ImageStat.Stat(gray).stddev[0] < UNIFORM_MAX_STDDEV:
        return True
    w, h = gray.size
    dx, dy = w // UNIFORM_CORE_INSET, h // UNIFORM_CORE_INSET
    core = gray.crop((dx, dy, w - dx, h - dy))
    return ImageStat.Stat(core).stddev[0] < UNIFORM_MAX_STDDEV


def _pixmap_image(pix):
    """Pixmap MuPDF -> image PIL, sans passer par un encodage intermédiaire."""
    from PIL import Image

    mode = {(1, 0): "L", (2, 1): "LA", (3, 0): "RGB", (4, 1): "RGBA"}[(pix.n, int(pix.alpha))]
    return Image.frombytes(mode, (pix.width, pix.height), bytes(pix.samples))


def _mupdf_errors(fitz) -> tuple:
    """Exceptions attendues d'un décodage MuPDF refusé (implémentations classique et rebasée)."""
    base = getattr(getattr(fitz, "mupdf", None), "FzErrorBase", None)
    return (RuntimeError, ValueError, KeyError) + ((base,) if isinstance(base, type) else ())


#: Doublon perceptuel : distance de Hamming maximale entre deux dHash 64 bits.
DUPLICATE_MAX_HAMMING = 6
#: Écart de proportions toléré : |ln(ratio_a / ratio_b)| au plus (≈ ±8 %).
DUPLICATE_MAX_LOG_ASPECT = 0.08
#: Seconde barrière : corrélation minimale des vignettes 32×32. Deux plans clairsemés
#: différents ont des dHash presque vides donc proches ; leurs traits, eux, ne coïncident pas.
DUPLICATE_MIN_CORRELATION = 0.90
_THUMB_SIDE = 32

#: Score (formule évaluée lors de la qualification).
SCORE_SURFACE_WEIGHT = 0.6
#: Surface affichée à partir de laquelle le terme de surface est plein (10 % de la page).
SCORE_SURFACE_FULL = 0.10
SCORE_RESOLUTION_WEIGHT = 0.4
#: Résolution : 0 à 20 000 px (seuil d'admission), 1 à partir de 1 000 000 px, log entre les deux.
SCORE_RESOLUTION_FLOOR_PX = 20_000
SCORE_RESOLUTION_CEIL_PX = 1_000_000
SCORE_BAND_PENALTY = 0.5
#: Bandeau : occurrence entièrement dans les 10 % haut ou bas de la page.
SCORE_BAND_FRACTION = 0.10
SCORE_REPEAT_PENALTY = 0.25

#: Sélection S4-prop : segments de pages, pénalité de diversité par visuel déjà retenu sur la page.
SELECTION_STRATEGY = "s4_prop_v1"
SELECTION_STRATEGY_SMALL = "discovery_order"
SELECTION_SEGMENTS = 4
SELECTION_PAGE_PENALTY = 0.3


@dataclass
class _Candidate:
    """Une image DISTINCTE (un xref) et ce que la passe 1 en a mesuré.

    Aucun pixel ni octet d'image n'y est conservé après la passe 1 : seulement des
    métadonnées, des mesures et une vignette 32×32 en niveaux de gris.
    """

    page: int
    xref: int
    bbox: Optional[Bbox]
    occurrences: List[Tuple[int, Optional[Bbox]]]
    #: Ordre de découverte (page, puis ordre de ``get_images``) : départage et tri final.
    discovery_index: int = 0
    #: xref du ``/SMask`` de l'image (0 : aucun). Jamais une source d'image à part entière.
    smask: int = 0
    #: Dimensions DÉCLARÉES de l'image principale (jamais celles du masque).
    width: int = 0
    height: int = 0
    #: Aire affichée maximale parmi les occurrences RÉELLES, en fraction de page [0, 1].
    surface_ratio: float = 0.0
    #: L'occurrence d'aire maximale est-elle entièrement dans un bandeau haut/bas ?
    in_band: bool = False
    #: Occurrences réelles du xref + membres de son groupe perceptuel (signal de score).
    repeat_count: int = 1
    #: Dimensions raster RÉELLES décodées (avant réduction).
    raster_width: int = 0
    raster_height: int = 0
    dhash: int = 0
    thumb: Optional[Tuple[int, ...]] = None
    score: float = 0.0
    segment: int = 0
    reject: str = ""
    #: ``embedded`` (xref) ou ``vector`` (région rendue; ``xref`` = 0).
    kind: str = "embedded"
    render_dpi: int = 0

    @property
    def aspect(self) -> float:
        return (self.raster_width / self.raster_height) if self.raster_height else 0.0


def _soft_masked_image(fitz, doc, cand: "_Candidate", *, max_pixels: int):
    """Pixels RÉELLEMENT affichés d'une image à ``/SMask`` : base + masque.

    Rend une image PIL avec canal alpha, ou ``None`` si la recomposition est impossible —
    l'appelant retombe alors sur le chemin historique (octets de base seuls), le
    filtre d'aplat s'appliquant ensuite comme à toute image.

    Un masque d'une autre définition que la base est REPORTÉ sur la grille de la base :
    le PDF affiche les deux sur le même rectangle.
    """
    from PIL import Image

    # Même borne que le décodage PIL défensif (qui refuse au-delà de 2 × 8 × max_pixels).
    if cand.width * cand.height > 16 * max(int(max_pixels), 1):
        return None
    try:
        base = fitz.Pixmap(doc, cand.xref)
        if base.alpha:
            base = fitz.Pixmap(base, 0)
        if base.colorspace is None or base.colorspace.n not in (1, 3):
            base = fitz.Pixmap(fitz.csRGB, base)
        mask = fitz.Pixmap(doc, cand.smask)
        if mask.alpha:
            mask = fitz.Pixmap(mask, 0)
        if mask.n != 1:
            mask = fitz.Pixmap(fitz.csGRAY, mask)
        if (mask.width, mask.height) == (base.width, base.height):
            return _pixmap_image(fitz.Pixmap(base, mask))
        img = _pixmap_image(base)
        alpha = _pixmap_image(mask).resize(img.size, Image.BILINEAR)
        img = img.convert("LA" if img.mode == "L" else "RGBA")
        img.putalpha(alpha)
        return img
    except _mupdf_errors(fitz) as e:
        logger.warning("pdf visual soft mask not applied code=smask_recompose_failed page=%s xref=%s "
                       "smask=%s size=%sx%s error=%s", cand.page, cand.xref, cand.smask,
                       cand.width, cand.height, e.__class__.__name__)
        return None


def _principal_geometry(occurrences, page_sizes: Dict[int, Tuple[float, float, float, float]]):
    """(surface affichée max, bandeau ?) sur les occurrences RÉELLES du xref."""
    best, band = 0.0, False
    for page, bbox in occurrences:
        rect = page_sizes.get(page)
        if bbox is None or rect is None:
            continue
        px0, py0, px1, py1 = rect
        area = (px1 - px0) * (py1 - py0)
        if area <= 0:
            continue
        ratio = min(max(((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / area, 0.0), 1.0)
        if ratio > best:
            h = py1 - py0
            best = ratio
            band = (bbox[3] - py0 <= SCORE_BAND_FRACTION * h) or (bbox[1] - py0 >= (1 - SCORE_BAND_FRACTION) * h)
    return best, band


def _collect(doc, *, stats: Dict, max_pages: int):
    """UNE itération de pages. Lecture seule, aucun rendu, aucune réouverture, aucun octet lu.

    Rend ``(candidats, géométrie des pages)``.
    """
    par_xref: Dict[int, _Candidate] = {}
    ordre: List[int] = []
    page_sizes: Dict[int, Tuple[float, float, float, float]] = {}
    pages = doc.page_count if max_pages <= 0 else min(doc.page_count, max_pages)
    for pno in range(pages):
        stats["pages_scanned"] += 1
        page = doc[pno]
        try:
            r = page.rect
            page_sizes[pno + 1] = (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
            infos = page.get_images(full=True)
        except Exception:  # noqa: BLE001 - un PDF hostile ne doit jamais casser l'ingestion
            continue
        for info in infos:
            xref = int(info[0])
            stats["images_seen"] += 1
            try:
                rects = page.get_image_rects(xref)
            except Exception:  # noqa: BLE001
                rects = []
            bbox = (tuple(round(float(v), 4) for v in rects[0]) if rects else None)
            if xref in par_xref:
                # MÊMES octets ailleurs dans le document : une occurrence de plus, pas
                # un second visuel. C'est le signal générique du logo/bandeau répété.
                par_xref[xref].occurrences.append((pno + 1, bbox))
                continue
            par_xref[xref] = _Candidate(page=pno + 1, xref=xref, bbox=bbox, occurrences=[],
                                        discovery_index=len(ordre), smask=int(info[1] or 0),
                                        width=int(info[2] or 0), height=int(info[3] or 0))
            ordre.append(xref)
    return [par_xref[x] for x in ordre], page_sizes


def _too_small(width: int, height: int, lim: PdfVisualLimits) -> bool:
    return min(width, height) < lim.min_side_px or (width * height) < lim.min_pixels


def _render(fitz, doc, cand: _Candidate, lim: PdfVisualLimits):
    """Pixels affichés, réduits au plafond : ``(image, largeur, hauteur raster)`` ou ``None``.

    UN SEUL chemin pour les deux passes : masque doux recomposé s'il existe, sinon
    chemin historique ``extract_image`` + PIL, INCHANGÉ (même décodeur, même empreinte).
    """
    if cand.kind == "vector":
        return _render_region(fitz, doc, cand, lim)
    img = (_soft_masked_image(fitz, doc, cand, max_pixels=lim.max_pixels)
           if cand.smask else None)
    if img is None:
        try:
            raw = doc.extract_image(cand.xref).get("image") or b""
        except Exception:  # noqa: BLE001
            raw = b""
        img = _decode(raw, max_pixels=lim.max_pixels) if raw else None
    if img is None:
        return None
    return _fit_pixels(img, lim.max_pixels), int(img.width), int(img.height)


def _render_region(fitz, doc, cand: _Candidate, lim: PdfVisualLimits):
    """Rendu de la SEULE région vectorielle : ``clip`` = bbox (repère non tourné) × ``rotation_matrix``,
    résolution réduite sous le plafond de pixels. ``(image, largeur, hauteur)`` ou ``None``."""
    x0, y0, x1, y1 = cand.bbox
    w_in, h_in = max(x1 - x0, 0.0) / 72.0, max(y1 - y0, 0.0) / 72.0
    if w_in <= 0 or h_in <= 0:
        return None
    dpi = config.pdf_vector_render_dpi()
    px = (w_in * dpi) * (h_in * dpi)
    if px > lim.max_pixels:
        dpi = max(36, int(dpi * math.sqrt(lim.max_pixels / px)))
    try:
        page = doc[cand.page - 1]
        clip = fitz.Rect(x0, y0, x1, y1) * page.rotation_matrix
        pix = page.get_pixmap(clip=clip, dpi=dpi, alpha=False, annots=False)
        img = _pixmap_image(pix)
    except Exception as e:  # noqa: BLE001 - un rendu refusé n'est jamais une erreur d'ingestion
        logger.warning("pdf vector region not rendered page=%s error=%s", cand.page, e.__class__.__name__)
        return None
    cand.render_dpi = dpi
    return _fit_pixels(img, lim.max_pixels), int(img.width), int(img.height)


def _vector_candidates(doc, candidats: List[_Candidate], *, stats: Dict, max_pages: int, regions,
                       lim: PdfVisualLimits) -> List[_Candidate]:
    """Vivier commun : images intégrées puis régions vectorielles, re-numérotées dans l'ordre documentaire."""
    from . import pdf_vector

    stats.update(pdf_vector.new_stats())
    stats["vector_raster_superseded"] = 0
    stats["vector_admitted"] = 0
    raster_rects: Dict[int, List[Bbox]] = {}
    for c in candidats:
        for page, bbox in [(c.page, c.bbox)] + list(c.occurrences):
            if bbox is not None:
                raster_rects.setdefault(page, []).append(bbox)
    try:
        found = pdf_vector.detect_vector_regions(
            doc, stats=stats, max_pages=max_pages, suppressed_regions=regions, raster_rects=raster_rects,
            table_overlap_ratio=lim.table_overlap_ratio, max_candidates=config.pdf_vector_max_candidates())
    except Exception as e:  # noqa: BLE001 - optionnel : les images intégrées restent intactes
        stats["vector_errors"] = stats.get("vector_errors", 0) + 1
        logger.warning("pdf vector detection failed error=%s", e.__class__.__name__)
        found = []
    vecteurs = [_Candidate(page=r.page, xref=0, bbox=r.bbox, occurrences=[], kind="vector") for r in found]
    pool = sorted(candidats + vecteurs,
                  key=lambda c: (c.page, 0 if c.kind == "embedded" else 1, c.discovery_index if c.kind == "embedded"
                                 else 0, (c.bbox or (0, 0, 0, 0))[1], (c.bbox or (0, 0, 0, 0))[0]))
    for i, c in enumerate(pool):
        c.discovery_index = i
    return pool


def _supersede_inner_rasters(pool: List[_Candidate], stats: Dict) -> List[_Candidate]:
    """Une image intégrée CONTENUE (≥ 90 %) dans une région vectorielle retenue est déjà dans son rendu."""
    vect = [c for c in pool if c.kind == "vector"]
    if not vect:
        return pool
    keep = []
    for c in pool:
        if c.kind == "embedded" and c.bbox is not None and any(
                v.page == c.page and _overlap_ratio(c.bbox, v.bbox) >= 0.9 for v in vect):
            stats["vector_raster_superseded"] += 1
            continue
        keep.append(c)
    return keep


def _measure(fitted) -> Tuple[int, Tuple[int, ...]]:
    """dHash 64 bits et vignette 32×32 en niveaux de gris, sur la composition sur blanc."""
    from PIL import Image

    from .image_extractor import flatten_rgb

    gray = flatten_rgb(fitted).convert("L")
    small = list(gray.resize((9, 8), Image.BOX).getdata())
    dhash = 0
    for row in range(8):
        for col in range(8):
            if small[row * 9 + col] > small[row * 9 + col + 1]:
                dhash |= 1 << (row * 8 + col)
    return dhash, tuple(gray.resize((_THUMB_SIDE, _THUMB_SIDE), Image.BOX).getdata())


def _score(cand: _Candidate) -> float:
    """Utilité PROBABLE, générique et explicable — aucune lecture de contenu métier.

    ``0.6 × min(surface / 0.10, 1) + 0.4 × résolution − 0.5 × bandeau − 0.25 × (répétitions − 1)``
    où résolution = ln(px / 20 000) / ln(1 000 000 / 20 000), bornée à [0, 1].
    """
    px = cand.raster_width * cand.raster_height
    resolution = 0.0
    if px > SCORE_RESOLUTION_FLOOR_PX:
        resolution = min(math.log(px / SCORE_RESOLUTION_FLOOR_PX)
                         / math.log(SCORE_RESOLUTION_CEIL_PX / SCORE_RESOLUTION_FLOOR_PX), 1.0)
    value = (SCORE_SURFACE_WEIGHT * min(cand.surface_ratio / SCORE_SURFACE_FULL, 1.0)
             + SCORE_RESOLUTION_WEIGHT * resolution
             - (SCORE_BAND_PENALTY if cand.in_band else 0.0)
             - SCORE_REPEAT_PENALTY * max(cand.repeat_count - 1, 0))
    return round(value, 6)


def _correlation(a: Sequence[int], b: Sequence[int]) -> float:
    n = len(a)
    if not n or n != len(b):
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / math.sqrt(va * vb)


def _is_visual_duplicate(a: _Candidate, b: _Candidate) -> bool:
    """Même contenu à une autre résolution ou compression ? Trois barrières conjointes."""
    if bin(a.dhash ^ b.dhash).count("1") > DUPLICATE_MAX_HAMMING:
        return False
    if a.aspect <= 0 or b.aspect <= 0 or abs(math.log(a.aspect / b.aspect)) > DUPLICATE_MAX_LOG_ASPECT:
        return False
    return _correlation(a.thumb or (), b.thumb or ()) >= DUPLICATE_MIN_CORRELATION


def _group_duplicates(pool: List[_Candidate]) -> Tuple[List[_Candidate], int]:
    """Un représentant par groupe perceptuel : meilleur score, puis plus petit index.

    Les membres écartés n'ajoutent AUCUNE provenance au représentant : seule sa
    répétition (signal de score) augmente.
    """
    reps: List[_Candidate] = []
    dropped = 0
    for cand in sorted(pool, key=lambda c: (-c.score, c.discovery_index)):
        rep = next((r for r in reps if _is_visual_duplicate(cand, r)), None)
        if rep is None:
            reps.append(cand)
            continue
        rep.repeat_count += 1
        cand.reject = "visual_duplicate"
        dropped += 1
    for rep in reps:
        rep.score = _score(rep)
    return sorted(reps, key=lambda c: c.discovery_index), dropped


def _largest_remainder(slots: int, weights: Sequence[float]) -> List[int]:
    """Répartit ``slots`` places proportionnellement à ``weights`` (plus forts restes).

    Égalité de reste : plus petit index. Poids tous nuls : répartition uniforme.
    """
    n = len(weights)
    if slots <= 0 or n == 0:
        return [0] * n
    total = sum(weights)
    if total <= 0:
        weights, total = [1.0] * n, float(n)
    exact = [slots * w / total for w in weights]
    alloc = [int(math.floor(x)) for x in exact]
    order = sorted(range(n), key=lambda i: (-(exact[i] - alloc[i]), i))
    for i in order[:slots - sum(alloc)]:
        alloc[i] += 1
    return alloc


def _select_s4_prop(pool: List[_Candidate], *, pages: int, budget: int, per_page: int) -> List[_Candidate]:
    """S4-prop : couverture minimale par segment, puis quotas ∝ masse de score.

    1. le document est découpé en ``SELECTION_SEGMENTS`` segments de pages égaux ;
    2. chaque segment non vide reçoit une place ;
    3. les places restantes suivent la masse de score (scores positifs) du segment,
       par la méthode des plus forts restes ;
    4. dans un segment, on retient itérativement le meilleur
       ``score − 0.3 × (visuels déjà retenus sur la même page)`` ;
    5. les places qu'un segment ne peut pas consommer reviennent au meilleur candidat
       restant du document, avec la même pénalité ;
    le plafond par page s'applique partout. Égalités : plus petit index de découverte.
    """
    segments = SELECTION_SEGMENTS
    pages = max(int(pages), 1)
    for cand in pool:
        cand.segment = min((cand.page - 1) * segments // pages, segments - 1)
    buckets = [[c for c in pool if c.segment == k] for k in range(segments)]
    nonempty = [k for k in range(segments) if buckets[k]][:budget]
    quota = dict.fromkeys(nonempty, 1)
    extra = _largest_remainder(budget - len(nonempty),
                               [sum(max(c.score, 0.0) for c in buckets[k]) for k in nonempty])
    for k, n in zip(nonempty, extra):
        quota[k] += n

    selected: List[_Candidate] = []
    taken: set = set()
    per_page_count: Dict[int, int] = {}

    def best(candidates):
        free = [c for c in candidates
                if c.discovery_index not in taken and per_page_count.get(c.page, 0) < per_page]
        if not free:
            return None
        return min(free, key=lambda c: (-(c.score - SELECTION_PAGE_PENALTY * per_page_count.get(c.page, 0)),
                                        c.discovery_index))

    def take(cand):
        selected.append(cand)
        taken.add(cand.discovery_index)
        per_page_count[cand.page] = per_page_count.get(cand.page, 0) + 1

    for k in nonempty:
        for _ in range(quota[k]):
            cand = best(buckets[k])
            if cand is None:
                break
            take(cand)
    while len(selected) < budget:
        cand = best(pool)
        if cand is None:
            break
        take(cand)
    return sorted(selected, key=lambda c: c.discovery_index)


def _analyse(fitz, doc, cand: _Candidate, *, lim: PdfVisualLimits, regions, page_sizes, stats) -> bool:
    """PASSE 1 pour un candidat : filtres puis mesures. Les pixels sont libérés au retour."""
    # 1. Décoratif : les mêmes octets répétés sur beaucoup de pages sont un logo,
    #    un bandeau ou un filigrane. Signal GÉNÉRIQUE (répétition d'empreinte),
    #    jamais un nom de fichier ni un texte de marque.
    if (len(cand.occurrences) + 1) > lim.max_repeats:
        stats["omitted_decorative"] += 1
        return False
    # 2. Recouvrement d'un tableau natif déjà extrait.
    if _covers_native_table(cand.bbox, regions.get(cand.page, ()), lim.table_overlap_ratio):
        stats["omitted_table_overlap"] += 1
        return False
    # 3. Trop petit pour porter une information : puce, filet, icône. Les dimensions
    #    DÉCLARÉES suffisent souvent à conclure, sans rien décoder ni recomposer.
    if cand.width > 0 and cand.height > 0 and _too_small(cand.width, cand.height, lim):
        stats["omitted_too_small"] += 1
        return False
    # 4. Pixels affichés (masque doux recomposé, sinon chemin historique).
    rendered = _render(fitz, doc, cand, lim)
    if rendered is None:
        stats["omitted_unreadable"] += 1
        return False
    fitted, cand.raster_width, cand.raster_height = rendered
    if _too_small(cand.raster_width, cand.raster_height, lim):
        stats["omitted_too_small"] += 1
        return False
    # 5. Aplat sans information (ombre portée, élément invisible) : jamais envoyé.
    try:
        uniform = _is_uniform(fitted)
    except (OSError, ValueError):  # mode PIL non mesurable : la normalisation tranchera
        uniform = False
    if uniform:
        stats["omitted_uniform"] += 1
        return False
    # 6. Mesures légères pour la sélection.
    try:
        cand.dhash, cand.thumb = _measure(fitted)
    except (OSError, ValueError):
        cand.dhash, cand.thumb = 0, None
    cand.surface_ratio, cand.in_band = _principal_geometry(
        [(cand.page, cand.bbox)] + list(cand.occurrences), page_sizes)
    cand.repeat_count = 1 + len(cand.occurrences)
    cand.score = _score(cand)
    return True


def _normalize(fitted):
    """Normalisation finale (PASSE 2 seulement). Point d'instrumentation des tests."""
    from .image_extractor import normalize_rgb_png

    return normalize_rgb_png(fitted)


def discover_pdf_visual_assets(
    data: bytes,
    *,
    suppressed_regions: Optional[Dict[int, List[Bbox]]] = None,
    limits: Optional[PdfVisualLimits] = None,
    max_pages: int = 0,
    vector_regions: bool = False,
) -> PdfVisualDiscovery:
    """Visuels intégrés ADMIS d'un PDF, prêts pour le rail visuel générique.

    ``suppressed_regions`` : régions tabulaires déjà extraites nativement (page 1-based
    -> bboxes), telles que le rail tabulaire générique les rend. Un visuel qui les
    recouvre est écarté : le natif fait foi (§8/§9 du lot).

    Best-effort et SANS exception : PyMuPDF absent, PDF illisible ou objet hostile
    rendent une découverte vide plutôt qu'une erreur — l'ingestion ordinaire ne doit
    jamais échouer à cause d'un visuel optionnel.
    """
    stats = _new_stats()
    out = PdfVisualDiscovery(assets=[], stats=stats)
    data = data or b""
    if not data:
        return out

    try:
        import fitz  # PyMuPDF
    except Exception:  # pragma: no cover - dépend de l'environnement
        logger.info("PyMuPDF indisponible : découverte visuelle PDF ignorée.")
        return out

    lim = limits or PdfVisualLimits.from_settings()
    regions = {int(p): [tuple(float(v) for v in b) for b in (boxes or [])]
               for p, boxes in (suppressed_regions or {}).items()}

    doc = None
    try:
        # OUVERTURE UNIQUE, au niveau document. Le document reste ouvert pendant les
        # deux passes : les pixels sont relus par xref, jamais conservés.
        doc = fitz.open(stream=data, filetype="pdf")
        stats["pdf_opens"] += 1
        candidats, page_sizes = _collect(doc, stats=stats, max_pages=max_pages)
        if vector_regions:
            candidats = _vector_candidates(doc, candidats, stats=stats, max_pages=max_pages, regions=regions,
                                           lim=lim)
    except Exception as e:  # noqa: BLE001
        logger.debug("pdf visual discovery skipped: %s", e.__class__.__name__)
        if doc is not None:
            doc.close()
        return out

    try:
        # PASSE 1 : filtres et mesures, un candidat à la fois.
        pool = [c for c in candidats
                if _analyse(fitz, doc, c, lim=lim, regions=regions, page_sizes=page_sizes, stats=stats)]
        if vector_regions:
            pool = _supersede_inner_rasters(pool, stats)
        budget = max(int(lim.max_assets), 0)
        strategy, chosen = SELECTION_STRATEGY_SMALL, pool
        if len(pool) > budget:
            # Budget contraint seulement : un PDF qui tient dans le budget garde TOUS ses
            # visuels, dans l'ordre de découverte (aucune suppression de quasi-doublon).
            pool, stats["omitted_visual_duplicate"] = _group_duplicates(pool)
            if len(pool) > budget:
                strategy = SELECTION_STRATEGY
                chosen = _select_s4_prop(pool, pages=stats["pages_scanned"], budget=budget,
                                         per_page=lim.max_per_page)
            else:
                chosen = pool
        stats["candidates_eligible"] = len(pool)
        stats["omitted_limit"] += len(pool) - len(chosen)
        # PASSE 2 : normalisation des seuls candidats retenus, dans l'ordre documentaire.
        _admit(fitz, doc, chosen, out=out, lim=lim)
        stats["selection"] = {"strategy": strategy, "eligible": len(pool),
                              "selected": stats["admitted"], "budget": budget,
                              "segments": SELECTION_SEGMENTS if strategy == SELECTION_STRATEGY else 0}
    finally:
        doc.close()
    return out


def _admit(fitz, doc, chosen: List[_Candidate], *, out: PdfVisualDiscovery, lim: PdfVisualLimits) -> None:
    """PASSE 2 : ``VisualAsset`` des candidats retenus, ordinal = rang d'admission."""
    from ..visual.capability import visual_task_for_source
    from ..visual.contracts import VisualAsset, VisualAssetError, VisualOccurrence, sha256_hex

    stats = out.stats
    par_page: Dict[int, int] = {}
    ordinal = 0
    for cand in sorted(chosen, key=lambda c: c.discovery_index):
        rendered = _render(fitz, doc, cand, lim)
        normalized = _normalize(rendered[0]) if rendered is not None else None
        if normalized is None:
            stats["omitted_unreadable"] += 1
            continue
        content, width, height = normalized
        if len(content) > lim.max_bytes:
            stats["omitted_too_large"] += 1
            continue

        # Plafonds : par page d'abord (une planche ne monopolise pas le budget), puis
        # global. Déjà respectés par S4-prop ; appliqués ici dans l'ordre de découverte
        # quand tout le vivier tient dans le budget. Dépassement = OMISSION comptée.
        if par_page.get(cand.page, 0) >= lim.max_per_page or ordinal >= lim.max_assets:
            stats["omitted_limit"] += 1
            continue

        try:
            asset = VisualAsset(
                content=content,
                mime_type="image/png",
                width=width,
                height=height,
                source_kind="pdf",
                content_hash=sha256_hex(content),
                ordinal=ordinal,
                source_page=cand.page,
                source_bbox=cand.bbox,
                source_locator=({"type": "pdf", "page": cand.page, "xref": cand.xref} if cand.kind == "embedded"
                                else {"type": "pdf", "page": cand.page, "region": "vector",
                                      "bbox": list(cand.bbox), "rotation": int(doc[cand.page - 1].rotation),
                                      "render_dpi": int(cand.render_dpi)}),
                origin="embedded" if cand.kind == "embedded" else "rendered",
                render_dpi=cand.render_dpi if cand.kind == "vector" else None,
                occurrences=tuple(
                    VisualOccurrence(source_page=p, source_bbox=b)
                    for p, b in cand.occurrences[:8]
                ),
                task=visual_task_for_source("pdf"),
            )
        except VisualAssetError as e:
            logger.debug("pdf visual asset rejected: %s", e)
            stats["omitted_unreadable"] += 1
            continue

        out.assets.append(asset)
        par_page[cand.page] = par_page.get(cand.page, 0) + 1
        ordinal += 1
        stats["admitted"] += 1
        if cand.kind == "vector":
            stats["vector_admitted"] += 1


def sha256_of(data: bytes) -> str:
    """Empreinte utilitaire (tests/diagnostic)."""
    return hashlib.sha256(data or b"").hexdigest()
