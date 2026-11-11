"""Capacité vision réellement QUALIFIÉE, par source.

L'architecture RAGatm est générique : le contrat Vision V1 définit six tâches et ce
dépôt sait normaliser toutes leurs charges utiles. La capacité du MODÈLE, elle, ne
l'est pas. La qualification AI-GATEWAY-VISION-1B ne gouverne aujourd'hui qu'une seule
tâche — ``describe`` — et le Gateway refuse les autres en échec FERMÉ
(HTTP 400 ``vision_task_not_qualified``, non relançable, aucune inférence).

Ce module porte ce fait, et rien d'autre :

* il ne réduit PAS le vocabulaire V1 (cf. ``vision_contract.TASKS``) ;
* il ne supprime aucun normaliseur (tableau, diagramme, graphique, dessin technique,
  mixte restent utiles aux capacités futures et aux tests de contrat) ;
* il n'est PAS réglable à l'exécution : la tâche servie est un fait de gouvernance,
  pas une préférence d'appelant ni un réglage d'exploitation ;
* aucun repli automatique vers une autre tâche, aucune devinette depuis l'extension,
  le texte OCR ou l'image, aucune sonde de capacités par document.

Changer la tâche d'une source doit rester un changement DÉLIBÉRÉ de ce dépôt, visible
en revue et couvert par un test.
"""
from __future__ import annotations

#: Tâches V1 que le Gateway est aujourd'hui autorisé à servir.
QUALIFIED_TASKS = frozenset({"describe"})

#: Tâches V1 valides au contrat mais NON qualifiées : le Gateway les refuse (400).
UNQUALIFIED_TASKS = frozenset({"auto", "table", "diagram", "chart", "technical_drawing"})

#: Tâche des images autonomes.
STANDALONE_IMAGE_VISUAL_TASK = "describe"

#: Tâche des visuels INTÉGRÉS d'un PDF. La même, et pour la même
#: raison : `describe` est la seule tâche qualifiée. Un visuel PDF peut bien être un
#: graphique ou un tableau à l'œil — tant que `chart`/`table` ne sont pas qualifiées,
#: il est traité comme une description factuelle en texte libre, et RIEN de plus.
PDF_VISUAL_TASK = "describe"

#: Tâche des visuels INTÉGRÉS d'un DOCX. La même, pour la même
#: raison : `describe` est la seule tâche qualifiée. Un visuel DOCX peut être un
#: graphique ou un organigramme à l'œil — il reste décrit en texte libre.
DOCX_VISUAL_TASK = "describe"

#: Tâche des images INTÉGRÉES d'une présentation PPTX. La même,
#: pour la même raison. Une image de diapositive représentant un tableau reste décrite
#: en texte libre : `table` n'est pas une capacité qualifiée.
PPTX_VISUAL_TASK = "describe"

#: Nature de source -> tâche opérationnelle. Toute source nouvelle doit s'ajouter
#: EXPLICITEMENT ici, avec sa propre qualification.
_TASK_BY_SOURCE: dict[str, str] = {
    "image": STANDALONE_IMAGE_VISUAL_TASK,
    "pdf": PDF_VISUAL_TASK,
    "docx": DOCX_VISUAL_TASK,
    "pptx": PPTX_VISUAL_TASK,
}


def visual_task_for_source(source_kind: str) -> str:
    """Tâche V1 à demander pour CETTE nature de source. Déterministe et fermée.

    Lève ``KeyError`` pour une source sans stratégie déclarée : mieux vaut un échec
    net qu'un repli silencieux sur une tâche non qualifiée.
    """
    try:
        return _TASK_BY_SOURCE[str(source_kind)]
    except KeyError:
        raise KeyError(
            f"aucune tâche vision qualifiée déclarée pour la source {source_kind!r}"
        ) from None
