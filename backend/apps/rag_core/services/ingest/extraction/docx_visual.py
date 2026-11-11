# backend/apps/rag_core/services/ingest/extraction/docx_visual.py
"""
Découverte des visuels INTÉGRÉS d'un DOCX.

ADAPTATEUR DE SOURCE, et rien d'autre : il transforme un paquet DOCX déjà ouvert en
``VisualAsset``. Il n'appelle pas le Gateway, ne normalise aucune réponse, ne publie
rien. L'analyse appartient au rail visuel générique déjà qualifié pour l'image
autonome et le PDF.

## Aucune ouverture supplémentaire

L'extracteur DOCX ouvre déjà l'archive (`zipfile.ZipFile`) et parse `word/document.xml`.
La découverte se greffe DANS ce même bloc, sur le MÊME handle et le MÊME arbre XML :
aucune réouverture du paquet, aucune réouverture par média, aucune seconde extraction
de texte ou de tableaux. Les compteurs `parts_read` / `package_opens_additional`
rendent cet invariant exécutable.

## Périmètre : le corps, comme le texte

L'extraction textuelle DOCX ne considère aujourd'hui QUE le corps du document
(`w:body`) : ni en-tête, ni pied, ni note, ni commentaire, ni zone de texte. Le
périmètre visuel suit STRICTEMENT le périmètre textuel — indexer la description d'une
image d'en-tête alors que le texte d'en-tête n'est pas indexé créerait une évidence
sans contexte, impossible à situer pour le lecteur.

## Pas de pagination

Un DOCX n'a pas de page avant d'être rendu. Aucun numéro de page n'est inventé :
``source_page`` reste ``None``, et la provenance est STRUCTURELLE (bloc, paragraphe,
tableau/ligne/cellule, identifiant de relation, partie du paquet).

## Ce que ce lot ne fait pas

Aucun rendu (ni DOCX→PDF, ni LibreOffice, ni capture), aucune interprétation
vectorielle (SmartArt, DrawingML de formes, EMF/WMF/SVG), aucune récupération réseau
d'une image liée. Les relations externes et les formes vectorielles sont omises avec
un compteur, jamais suivies.
"""
from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from . import config

logger = logging.getLogger(__name__)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_RELS_PART = "word/_rels/document.xml.rels"
_DOCUMENT_DIR = "word"

#: Types de relation acceptés : uniquement l'image. Tout autre type est ignoré.
_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


@dataclass(frozen=True)
class DocxVisualLimits:
    """Bornes génériques. RÉUTILISE les réglages visuels existants plutôt que d'en
    inventer de nouveaux : les seuils (côté minimal, pixels, octets, répétitions,
    plafond par document) sont sémantiquement les mêmes que pour le PDF. Le nom des
    réglages reste historiquement préfixé `DOCUMENT_PDF_VISUAL_` — dette de nommage
    assumée, préférable à huit réglages en double."""

    max_assets: int = 12
    min_side_px: int = 96
    min_pixels: int = 20_000
    max_pixels: int = 2_000_000
    max_bytes: int = 8_000_000
    max_repeats: int = 3
    max_occurrences: int = 64

    @classmethod
    def from_settings(cls) -> "DocxVisualLimits":
        return cls(
            max_assets=config.pdf_visual_max_assets(),
            min_side_px=config.pdf_visual_min_side_px(),
            min_pixels=config.pdf_visual_min_pixels(),
            max_pixels=config.pdf_visual_max_pixels(),
            max_bytes=config.pdf_visual_max_bytes(),
            max_repeats=config.pdf_visual_max_repeats(),
        )


@dataclass
class DocxVisualDiscovery:
    assets: List = field(default_factory=list)
    stats: Dict = field(default_factory=dict)


def _new_stats() -> Dict:
    return {
        "package_opens_additional": 0,
        "parts_read": 0,
        "drawings_seen": 0,
        "occurrences": 0,
        "relationships_resolved": 0,
        "admitted": 0,
        "omitted_external": 0,
        "omitted_unresolved": 0,
        "omitted_unreadable": 0,
        "omitted_too_small": 0,
        "omitted_too_large": 0,
        "omitted_decorative": 0,
        "omitted_limit": 0,
    }


@dataclass
class _Occurrence:
    """Emplacement STRUCTUREL d'un visuel dans le corps. Aucune page, jamais."""

    block: int
    kind: str                      # "paragraph" | "table"
    paragraph: Optional[int] = None
    table: Optional[int] = None
    row: Optional[int] = None
    cell: Optional[int] = None
    anchored: bool = False         # wp:anchor (flottant) vs wp:inline

    def as_locator(self, *, rel_id: str, part: str) -> dict:
        loc = {
            "type": "docx",
            "story": "body",
            "block": self.block,
            "rel_id": rel_id,
            "part": part,
            "anchored": self.anchored,
        }
        for nom in ("paragraph", "table", "row", "cell"):
            valeur = getattr(self, nom)
            if valeur is not None:
                loc[nom] = valeur
        return loc


def _blips(element: ET.Element) -> List[Tuple[str, bool]]:
    """``(rel_id, flottant)`` des images DrawingML d'un élément.

    On lit ``a:blip/@r:embed`` — la référence d'une image EMBARQUÉE. ``@r:link``
    désigne une image LIÉE (externe) : elle est comptée à part et jamais suivie.
    VML hérité (``w:pict``/``v:imagedata``) n'est pas traité : il sort du périmètre
    déclaré de ce lot.
    """
    trouves: List[Tuple[str, bool]] = []
    for drawing in element.iter(f"{_W}drawing"):
        flottant = any(True for _ in drawing.iter(
            "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}anchor"))
        for blip in drawing.iter(f"{_A}blip"):
            rel = blip.get(f"{_R}embed")
            if rel:
                trouves.append((rel, flottant))
    return trouves


def _external_links(element: ET.Element) -> int:
    """Nombre d'images LIÉES (``a:blip/@r:link``) — omises, jamais récupérées."""
    n = 0
    for drawing in element.iter(f"{_W}drawing"):
        for blip in drawing.iter(f"{_A}blip"):
            if blip.get(f"{_R}link") and not blip.get(f"{_R}embed"):
                n += 1
    return n


def _walk_body(body: ET.Element, stats: Dict) -> Tuple[Dict[str, List[_Occurrence]], int]:
    """Parcourt le corps DANS L'ORDRE DU DOCUMENT et relève les occurrences.

    Aucune seconde extraction : on ne lit ni le texte ni les tableaux, seulement la
    position structurelle des images.
    """
    par_rel: Dict[str, List[_Occurrence]] = {}
    externes = 0
    i_par = 0
    i_tbl = 0

    def _ajouter(rel: str, occ: _Occurrence) -> None:
        par_rel.setdefault(rel, []).append(occ)
        stats["occurrences"] += 1

    for bloc, child in enumerate(list(body)):
        if child.tag == f"{_W}p":
            externes += _external_links(child)
            for rel, flottant in _blips(child):
                stats["drawings_seen"] += 1
                _ajouter(rel, _Occurrence(block=bloc, kind="paragraph",
                                          paragraph=i_par, anchored=flottant))
            i_par += 1
        elif child.tag == f"{_W}tbl":
            for i_ligne, tr in enumerate(child.findall(f"{_W}tr")):
                for i_cell, tc in enumerate(tr.findall(f"{_W}tc")):
                    externes += _external_links(tc)
                    for rel, flottant in _blips(tc):
                        stats["drawings_seen"] += 1
                        _ajouter(rel, _Occurrence(block=bloc, kind="table", table=i_tbl,
                                                  row=i_ligne, cell=i_cell,
                                                  anchored=flottant))
            i_tbl += 1

    return par_rel, externes


def _read_relationships(zf, stats: Dict) -> Dict[str, dict]:
    """``rel_id -> {target, external}`` depuis ``word/_rels/document.xml.rels``.

    Seules les relations de TYPE image sont retenues : le graphe de relations fait
    foi, jamais un balayage de ``word/media/*`` — un paquet peut contenir des médias
    orphelins, ou des médias possédés par un en-tête hors périmètre.
    """
    from .xml_security import reject_deeply_nested_xml, reject_unsafe_xml
    from .zip_security import read_bounded_entry

    brut = read_bounded_entry(zf, _RELS_PART, max_bytes=config.docx_max_uncompressed_bytes())
    if not brut:
        return {}
    stats["parts_read"] += 1
    try:
        reject_unsafe_xml(brut, part_name=_RELS_PART)
        reject_deeply_nested_xml(brut, part_name=_RELS_PART)
        racine = ET.fromstring(brut)
    except Exception:  # noqa: BLE001 - une relation illisible ne casse pas l'ingestion
        logger.debug("docx relationships unreadable: part=%s", _RELS_PART)
        return {}

    out: Dict[str, dict] = {}
    for rel in racine.iter(f"{_PKG_REL}Relationship"):
        rid = rel.get("Id")
        if not rid or rel.get("Type") != _IMAGE_REL_TYPE:
            continue
        out[rid] = {
            "target": rel.get("Target") or "",
            "external": (rel.get("TargetMode") or "").lower() == "external",
        }
    return out


def _part_path(target: str) -> Optional[str]:
    """Chemin de partie du paquet pour une cible de relation INTERNE.

    Refuse tout ce qui sort du paquet : cible absolue, schéma d'URL, ou remontée
    au-dessus de la racine après normalisation.
    """
    cible = (target or "").strip().replace("\\", "/")
    if not cible or "://" in cible or cible.startswith("/"):
        return None
    chemin = posixpath.normpath(posixpath.join(_DOCUMENT_DIR, cible))
    if chemin.startswith("../") or chemin == ".." or chemin.startswith("/"):
        return None
    return chemin


def _decode(raw: bytes, *, max_pixels: int):
    """Décodage DÉFENSIF d'un média intégré (bombe de décompression, format exotique)."""
    from PIL import Image

    prev = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max(int(max_pixels) * 8, 1)
    try:
        img = Image.open(BytesIO(raw))
        img.load()
        return img
    except Exception:  # noqa: BLE001
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = prev


def _fit_pixels(img, max_pixels: int):
    """Réduction DÉTERMINISTE au-dessus du plafond de pixels du contrat V1."""
    from PIL import Image

    w, h = int(img.width), int(img.height)
    if max_pixels <= 0 or (w * h) <= max_pixels:
        return img
    facteur = (max_pixels / float(w * h)) ** 0.5
    return img.resize((max(1, int(w * facteur)), max(1, int(h * facteur))), Image.LANCZOS)


def discover_docx_visual_assets(zf, root: ET.Element, *,
                                limits: Optional[DocxVisualLimits] = None) -> DocxVisualDiscovery:
    """Visuels intégrés ADMIS du corps d'un DOCX, prêts pour le rail visuel générique.

    ``zf`` : l'archive DÉJÀ ouverte par l'extracteur — aucune réouverture.
    ``root`` : l'arbre DÉJÀ parsé de ``word/document.xml`` — aucun second parsing.

    Best-effort et SANS exception : relations absentes, média illisible ou objet
    hostile rendent une découverte vide plutôt qu'une erreur. L'ingestion ordinaire ne
    doit jamais échouer à cause d'un visuel optionnel.
    """
    from ..visual.capability import visual_task_for_source
    from ..visual.contracts import VisualAsset, VisualAssetError, VisualOccurrence, sha256_hex
    from .image_extractor import normalize_rgb_png
    from .zip_security import read_bounded_entry

    stats = _new_stats()
    out = DocxVisualDiscovery(assets=[], stats=stats)

    body = root.find(f"{_W}body")
    if body is None:
        return out

    lim = limits or DocxVisualLimits.from_settings()

    par_rel, externes = _walk_body(body, stats)
    stats["omitted_external"] += externes
    if not par_rel:
        return out

    relations = _read_relationships(zf, stats)

    # Regroupement par OCTETS : un même média référencé plusieurs fois n'est analysé
    # qu'une fois, mais toutes ses occurrences structurelles sont conservées.
    par_hash: Dict[str, dict] = {}
    ordre: List[str] = []

    for rel_id, occurrences in par_rel.items():
        info = relations.get(rel_id)
        if info is None:
            stats["omitted_unresolved"] += 1
            continue
        if info["external"]:
            # Image LIÉE : jamais récupérée, aucun accès réseau ni fichier.
            stats["omitted_external"] += 1
            continue
        chemin = _part_path(info["target"])
        if chemin is None:
            stats["omitted_unresolved"] += 1
            continue

        brut = read_bounded_entry(zf, chemin, max_bytes=lim.max_bytes)
        if not brut:
            stats["omitted_unreadable"] += 1
            continue
        stats["parts_read"] += 1
        stats["relationships_resolved"] += 1

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
        entree = par_hash.get(empreinte)
        if entree is None:
            entree = {"content": contenu, "width": largeur, "height": hauteur,
                      "rel_id": rel_id, "part": chemin, "occurrences": []}
            par_hash[empreinte] = entree
            ordre.append(empreinte)
        entree["occurrences"].extend(occurrences)

    for empreinte in ordre:
        e = par_hash[empreinte]
        occurrences = e["occurrences"]

        # Répétition d'octets identiques : logo, bandeau, filigrane. Signal GÉNÉRIQUE,
        # jamais un nom de fichier ni un texte de marque.
        if len(occurrences) > lim.max_repeats:
            stats["omitted_decorative"] += 1
            continue
        if stats["admitted"] >= lim.max_assets:
            stats["omitted_limit"] += 1
            continue

        premiere = occurrences[0]
        try:
            asset = VisualAsset(
                content=e["content"],
                mime_type="image/png",
                width=e["width"],
                height=e["height"],
                source_kind="docx",
                content_hash=empreinte,
                ordinal=stats["admitted"],
                # Un DOCX n'a pas de page avant rendu : aucun numéro n'est inventé.
                source_page=None,
                source_bbox=None,
                source_locator=premiere.as_locator(rel_id=e["rel_id"], part=e["part"]),
                origin="embedded",
                occurrences=tuple(
                    VisualOccurrence(source_page=None, source_bbox=None)
                    for _ in occurrences[1:lim.max_occurrences]
                ),
                task=visual_task_for_source("docx"),
            )
        except VisualAssetError as exc:
            logger.debug("docx visual asset rejected: %s", exc)
            stats["omitted_unreadable"] += 1
            continue

        out.assets.append(asset)
        stats["admitted"] += 1

    return out
