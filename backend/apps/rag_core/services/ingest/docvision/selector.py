# backend/apps/rag_core/services/ingest/docvision/selector.py
from __future__ import annotations

"""
Choix de l'autorité raster entre MinerU et le chemin OCR actuel.

Le critère n'est **pas** le volume de texte. Mesure à l'appui : sur la page qui semblait
régresser, MinerU rendait 308 caractères contre 483, et une règle sur le nombre de
caractères aurait déclenché un repli à tort — parce qu'elle comparait les seuls blocs de
MinerU au texte complet de Tesseract, en ignorant le tableau que MinerU avait structuré.
Sortie complète contre sortie complète, MinerU donnait 618 caractères et 52 mots
distincts contre 483 et 37.

Le critère retenu est donc le **vocabulaire distinct**, sur la sortie COMPLÈTE de chaque
moteur. Il répond à la bonne question : ce moteur a-t-il lu des choses que l'autre n'a
pas lues ? Un texte plus court mais aussi riche n'est pas un texte moins bon.

Aucune fusion des deux textes : un seul moteur fait autorité pour une page raster. Mêler
les deux garantirait la duplication qu'on a passé un lot entier à éliminer.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

_MOT = re.compile(r"[0-9A-Za-zÀ-ɏ]{2,}")


def mots_distincts(texte: str) -> int:
    """Vocabulaire distinct, casse repliée. Les jetons d'un seul caractère sont ignorés :
    ils sont majoritairement du bruit d'OCR et gonfleraient les deux moteurs à la fois."""
    return len({m.lower() for m in _MOT.findall(texte or "")})


def texte_complet(texte_blocs: str, tables: Optional[Sequence[dict]] = None) -> str:
    """Sortie COMPLÈTE d'un moteur : ses blocs de texte ET ses tableaux.

    Comparer le texte complet d'un moteur aux seuls blocs de l'autre est l'erreur qui a
    fait conclure à une régression inexistante.
    """
    parts = [texte_blocs or ""]
    for t in tables or []:
        md = t.get("markdown") if isinstance(t, dict) else None
        if md:
            parts.append(md)
    return "\n".join(p for p in parts if p)


@dataclass
class Selection:
    texte: str
    tables: List[dict] = field(default_factory=list)
    #: blocs géométriques du moteur retenu — vides quand Tesseract fait autorité,
    #: car le chemin OCR actuel ne produit aucune bbox et on n'en invente pas.
    blocs: List[Any] = field(default_factory=list)
    moteur: str = "tesseract"
    raison: str = ""
    #: compteurs de décision — jamais de contenu documentaire
    mesures: Dict[str, Any] = field(default_factory=dict)


def select_raster_extraction(
    mineru: Optional[Dict[str, Any]],
    tesseract: Optional[Dict[str, Any]],
    page_meta: Optional[Dict[str, Any]] = None,
) -> Selection:
    """Choisit l'autorité raster d'UNE page.

    `mineru` : {"texte": str, "tables": [...], "blocs": [...], "erreur": str|None}
    `tesseract` : {"texte": str} — sortie DÉJÀ produite par le pipeline, jamais relancée.
    """
    page_meta = page_meta or {}
    t_texte = (tesseract or {}).get("texte") or ""
    erreur = (mineru or {}).get("erreur")
    m_blocs = (mineru or {}).get("texte") or ""
    m_tables = list((mineru or {}).get("tables") or [])
    m_blocs_geo = list((mineru or {}).get("blocs") or [])
    m_complet = texte_complet(m_blocs, m_tables)

    base = {"mineru_chars": len(m_complet), "tesseract_chars": len(t_texte),
            "page_number": page_meta.get("page_number")}

    if erreur:
        return Selection(texte=t_texte, moteur="tesseract",
                         raison=f"moteur en echec ({erreur})",
                         mesures={**base, "erreur": erreur})
    if not m_complet.strip():
        return Selection(texte=t_texte, moteur="tesseract",
                         raison="sortie moteur vide", mesures=base)

    m_mots, t_mots = mots_distincts(m_complet), mots_distincts(t_texte)
    base.update({"mineru_distinct_words": m_mots, "tesseract_distinct_words": t_mots})

    if m_mots >= t_mots:
        # L'égalité va à MinerU : à vocabulaire égal, il apporte en plus la géométrie
        # et la provenance, que le chemin actuel ne produit pas du tout.
        return Selection(texte=m_blocs, tables=m_tables, blocs=m_blocs_geo,
                         moteur="mineru",
                         raison=("vocabulaire distinct au moins egal"
                                 if m_mots > t_mots else
                                 "vocabulaire distinct egal, geometrie en plus"),
                         mesures=base)
    return Selection(texte=t_texte, moteur="tesseract",
                     raison="vocabulaire distinct inferieur", mesures=base)
