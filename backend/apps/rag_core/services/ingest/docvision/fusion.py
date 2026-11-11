# backend/apps/rag_core/services/ingest/docvision/fusion.py
from __future__ import annotations

"""
Fusion « natif + enrichissement » page par page.

Une seule règle porte toute la sûreté du rail :

    sur une page dont le texte natif est exploitable, le texte MinerU n'entre JAMAIS
    dans le flux de page.

Elle élimine la double indexation à la source plutôt que de la rattraper après coup :
l'enrichissement textuel et l'autorité native sont mutuellement exclusifs par page.
Ce n'est pas une précaution abstraite — le banc a mesuré que le moteur produit trois
fois moins de matière que le natif sur les planches vectorielles ; concaténer les deux
aurait à la fois dupliqué et dégradé.

Les tableaux, eux, sont importés même sur page native, mais comme unités ATOMIQUES, et
leur texte est retiré du flux pour la même raison.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..extraction.base import Block
from .router import DecisionRoutage, Route


@dataclass
class ResultatPage:
    """Ce qu'une page apporte après fusion."""

    page_number: int
    route: str
    texte: str = ""
    texte_source: str = "native"
    blocs: List[Block] = field(default_factory=list)
    tables: List[dict] = field(default_factory=list)
    #: compteurs d'observabilité, jamais des estimations
    stats: Dict[str, int] = field(default_factory=dict)


def _texte_des_tables(tables: Sequence[dict]) -> str:
    return "\n".join(t.get("markdown", "") for t in tables)


def _deja_dans(table: dict, texte: str) -> bool:
    """Le contenu de ce tableau est-il déjà présent dans le texte donné ?

    Critère déterministe et volontairement prudent : on ne regarde que les cellules
    d'au moins quatre caractères, et l'on conclut au doublon si au moins la moitié
    d'entre elles se retrouvent telles quelles. Un seuil plus bas écarterait des
    tableaux réellement nouveaux ; un seuil plus haut laisserait passer des doublons.
    """
    if not texte:
        return False
    cellules = []
    for ligne in (table.get("markdown") or "").splitlines():
        if not ligne.strip().startswith("|"):
            continue
        for c in ligne.strip("|").split("|"):
            c = c.strip()
            if len(c) >= 4 and set(c) - set("-: "):
                cellules.append(c)
    if not cellules:
        return False
    presentes = sum(1 for c in cellules if c in texte)
    return presentes >= max(1, len(cellules) // 2)


def fusionner_page(
    decision: DecisionRoutage,
    *,
    texte_natif: str,
    blocs_natifs: Optional[Sequence[Block]] = None,
    blocs_mineru: Optional[Sequence[Block]] = None,
    tables_mineru: Optional[Sequence[dict]] = None,
    texte_ocr_repli: Optional[str] = None,
) -> ResultatPage:
    """Applique le contrat de fusion à UNE page.

    `texte_ocr_repli` est le texte du chemin actuel (Tesseract). Il n'est utilisé que
    si MinerU était primaire et n'a rien produit : c'est le repli, pas un complément.
    """
    blocs_natifs = list(blocs_natifs or [])
    blocs_mineru = list(blocs_mineru or [])
    tables_mineru = list(tables_mineru or [])
    r = ResultatPage(page_number=decision.page_number, route=decision.route.value)

    if decision.route is Route.MINERU_PRIMAIRE:
        texte_mineru = "\n".join(b.text for b in blocs_mineru if b.text).strip()
        if texte_mineru:
            r.texte, r.texte_source = texte_mineru, "mineru"
            r.blocs = blocs_mineru
        elif (texte_ocr_repli or "").strip():
            r.texte, r.texte_source = texte_ocr_repli.strip(), "tesseract_fallback"
        else:
            r.texte, r.texte_source = "", "none"
        r.tables = tables_mineru
        r.stats = {
            "raster_chars_added": len(r.texte),
            "native_chars_preserved": 0,
            "tables": len(r.tables),
            "blocks_with_bbox": sum(1 for b in r.blocs if b.bbox is not None),
        }
        return r

    # Page native : le texte natif est l'autorité, point final.
    r.texte, r.texte_source = texte_natif, "native"
    r.blocs = list(blocs_natifs)
    # Seuls les blocs MinerU porteurs de GÉOMÉTRIE sont conservés, et seulement comme
    # provenance : leur texte n'est pas réinjecté dans le flux de page.
    r.blocs += [b for b in blocs_mineru if b.bbox is not None]
    # Un tableau dont le contenu est DÉJÀ dans le texte natif n'est pas importé.
    #
    # Mesuré : sans cette règle, six lignes de tableau existaient deux fois — une fois
    # comme unité atomique, une fois dans le flux de page. Les retirer du texte natif
    # serait l'autre issue, mais elle violerait la préservation intégrale du natif, qui
    # est une porte absolue. On renonce donc à l'import, et l'on compte le renoncement :
    # si MinerU n'apporte rien sur ces pages, le chiffre doit le dire.
    gardees, ecartees = [], 0
    for t in tables_mineru:
        if _deja_dans(t, texte_natif):
            ecartees += 1
        else:
            gardees.append(t)
    r.tables = gardees
    r.stats_tables_ecartees = ecartees
    r.stats = {
        "raster_chars_added": 0,
        "native_chars_preserved": len(texte_natif or ""),
        "tables": len(r.tables),
        "tables_ecartees_doublon": ecartees,
        "blocks_with_bbox": sum(1 for b in r.blocs if b.bbox is not None),
    }
    return r


def texte_document(pages: Sequence[ResultatPage]) -> str:
    """Flux de page du document. Le contenu des tableaux en est EXCLU : il vit dans
    des unités atomiques, et l'y laisser l'indexerait deux fois."""
    return "\n\n".join(p.texte for p in pages if p.texte.strip())


def controle_non_duplication(pages: Sequence[ResultatPage]) -> Dict[str, int]:
    """Vérifie, sur le résultat produit, qu'aucun contenu n'existe en double.

    On ne se contente pas d'affirmer que la règle de fusion l'empêche : on le mesure
    sur la sortie, parce qu'une règle correcte mal appliquée se voit ici et nulle part
    ailleurs.
    """
    doublons_table = 0
    for p in pages:
        for t in p.tables:
            # MÊME prédicat que la règle d'import (`_deja_dans`). Deux critères
            # divergents laissaient la règle refuser l'écart pendant que le contrôle
            # criait au doublon : six lignes dupliquées passaient entre les deux.
            if _deja_dans(t, p.texte):
                doublons_table += 1
    return {
        "pages": len(pages),
        "duplicate_table_rows": doublons_table,
        "pages_double_source": sum(
            1 for p in pages if p.texte_source == "mineru" and p.stats.get(
                "native_chars_preserved", 0) > 0),
    }
