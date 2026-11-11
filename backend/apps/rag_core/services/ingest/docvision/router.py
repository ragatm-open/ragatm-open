# backend/apps/rag_core/services/ingest/docvision/router.py
from __future__ import annotations

"""
Routage PAGE PAR PAGE du rail hybride.

Aucun classifieur n'est créé. La décision « cette page a-t-elle un texte natif
exploitable ? » **existe déjà** dans l'extracteur PDF, et c'est exactement celle dont
le rail a besoin :

    len(texte.strip()) < DOCUMENT_OCR_MIN_CHARS_PER_PAGE  ou  is_unusable_pdf_text(texte)

Le second terme n'est pas un détail : c'est lui qui attrape les PDF nés numériques dont
l'encodage est cassé (octets bruts sans table /ToUnicode). Un tel document a bien une
couche texte, elle est simplement illisible — et un routage fondé sur le seul nombre de
caractères le classerait « natif exploitable » à tort.

Le routage est page-level et non document-level parce que la mesure l'impose : un même
document porte des pages où le natif produit dix fois plus de matière que le moteur, et
des pages où il n'en produit aucune.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..extraction import config as extraction_config
from ..extraction.text_quality import is_unusable_pdf_text, usable_chars
from . import config


class Route(str, Enum):
    #: page laissée entièrement à la chaîne native
    NATIF = "native"
    #: texte natif conservé, MinerU appelé pour tables et géométrie seulement
    NATIF_ENRICHI = "native_enriched"
    #: pas de texte natif exploitable : MinerU devient OCR primaire
    MINERU_PRIMAIRE = "mineru_primary"


@dataclass
class DecisionRoutage:
    page_number: int
    route: Route
    raison: str
    caracteres_utiles: int = 0
    texte_inexploitable: bool = False
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"page_number": self.page_number, "route": self.route.value,
                "raison": self.raison, "caracteres_utiles": self.caracteres_utiles,
                "texte_inexploitable": self.texte_inexploitable, **self.meta}


def texte_natif_exploitable(texte: Optional[str], *, seuil: Optional[int] = None) -> bool:
    """Reprend TEL QUEL le critère de l'extracteur PDF. Ne pas le redéfinir ici : deux
    définitions divergentes du même prédicat produiraient un routage incohérent avec la
    décision OCR prise quelques lignes plus loin dans le pipeline."""
    t = texte or ""
    s = seuil if seuil is not None else extraction_config.ocr_min_chars_per_page()
    return not (len(t.strip()) < s or is_unusable_pdf_text(t))


def router_page(page_number: int, texte_natif: Optional[str], *,
                enrichir_natif: bool = True) -> DecisionRoutage:
    """Décide le chemin d'UNE page, avant tout appel au moteur."""
    t = texte_natif or ""
    seuil = extraction_config.ocr_min_chars_per_page()
    court = len(t.strip()) < seuil
    # `is_unusable_pdf_text` répond True sur une page vide : sans cet ordre, une page
    # sans texte serait diagnostiquée « encodage cassé », ce qui enverrait sur une
    # fausse piste quiconque lit le journal de routage.
    inexploitable = (not court) and is_unusable_pdf_text(t)
    utiles = usable_chars(t)
    if court or inexploitable:
        return DecisionRoutage(
            page_number=page_number, route=Route.MINERU_PRIMAIRE,
            raison=(f"texte natif insuffisant (<{seuil} caractères)" if court
                    else "texte natif inexploitable (encodage)"),
            caracteres_utiles=utiles, texte_inexploitable=inexploitable,
        )
    if enrichir_natif:
        return DecisionRoutage(
            page_number=page_number, route=Route.NATIF_ENRICHI,
            raison="texte natif exploitable : conservé comme autorité, enrichissement seul",
            caracteres_utiles=utiles,
        )
    return DecisionRoutage(page_number=page_number, route=Route.NATIF,
                           raison="rail désactivé pour cette page",
                           caracteres_utiles=utiles)


def router_image_autonome(page_number: int = 1) -> DecisionRoutage:
    """Fichier image : il n'y a par construction aucun texte natif.

    Le chemin historique envoie l'image à Tesseract sans condition, sans même consulter
    le drapeau OCR. Sous rail actif, MinerU devient primaire et Tesseract le repli ;
    rail éteint, le chemin historique est intact.
    """
    return DecisionRoutage(page_number=page_number, route=Route.MINERU_PRIMAIRE,
                           raison="fichier image : aucune couche texte par construction",
                           caracteres_utiles=0)


def rail_actif() -> bool:
    return config.hybrid_enabled()


def router_page_post_extraction(page_number: int, texte: Optional[str], *,
                                ocr_applique: bool) -> DecisionRoutage:
    """Routage APRÈS extraction, quand le texte OCR a déjà remplacé celui de la page.

    `router_page` juge du texte NATIF. À ce stade il n'en reste rien : une page scannée
    porte un texte OCR long et propre, que ce prédicat classerait « natif exploitable ».
    Le rail ne router­ait alors jamais une page raster — exactement les pages pour
    lesquelles il existe. La seule source fiable est la trace d'extraction
    `metadata["ocr_pages"]`, qui dit quelles pages ont été rastérisées.
    """
    if ocr_applique:
        return DecisionRoutage(
            page_number=page_number, route=Route.MINERU_PRIMAIRE,
            raison="page raster : OCR appliqué pendant l'extraction",
            caracteres_utiles=usable_chars(texte or ""),
        )
    return router_page(page_number, texte)
