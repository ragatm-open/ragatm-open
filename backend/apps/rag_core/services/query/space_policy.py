# backend/apps/rag_core/services/query/space_policy.py
from __future__ import annotations

"""
Résolution du seuil de pertinence vectorielle, PAR ESPACE (P3-d).

Pourquoi
--------
`RAG_MIN_SCORE` est un seuil ABSOLU appliqué à une similarité cosinus. Cette
similarité n'a pas la même échelle partout : elle dépend du modèle d'embedding et de
l'appariement linguistique entre la question et le fonds.

Mesuré sur le corpus de qualification (questions françaises, documents anglais) :

- documents PERTINENTS  : 0,727 – 0,739
- questions HORS SUJET  : 0,662 – 0,687
- fenêtre séparatrice   : ]0,687 ; 0,727[
- défaut global         : 0,76 — **au-dessus des deux populations**

Le seuil ne discriminait donc plus rien sur cet espace : il coupait tout, y compris des
documents classés premiers par le leg vectoriel. Ce n'est pas un défaut du retrieval,
c'est un défaut de CALIBRAGE, et le calibrage appartient à l'espace — c'est lui qui
porte le couple (modèle d'embedding, langue du fonds).

Ce que ce module ne fait pas
----------------------------
Il ne BAISSE aucun seuil. Le défaut global reste inchangé, et sans valeur explicite sur
l'espace ni flag ouvert, la valeur résolue est exactement celle d'aujourd'hui.
"""

import logging
from typing import Any, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# Bornes admissibles. Une similarité cosinus normalisée vit dans [0, 1] ; au-delà, la
# valeur est une erreur de saisie et non un réglage.
MIN_SCORE_FLOOR = 0.0
MIN_SCORE_CEILING = 1.0


def space_min_score_enabled() -> bool:
    return bool(getattr(settings, "RAG_SPACE_MIN_SCORE_ENABLED", False))


def _resolve_with_source(rag_space: Any) -> tuple[float, str]:
    """Seuil applicable ET sa provenance, décidés au MÊME endroit.

    Les deux sont renvoyés ensemble à dessein. Les recalculer séparément avait produit
    un défaut d'étiquetage : une valeur d'espace REJETÉE (hors bornes ou non numérique)
    était annoncée comme un `space_override`, parce que la provenance était re-dérivée
    de `min_score is not None` sans savoir si la valeur avait été retenue. L'exploitant
    qui a saisi `1.5` lisait « l'espace a tranché » alors que sa saisie avait été
    écartée — précisément le cas où il faut l'alerter.
    """
    default = float(getattr(settings, "RAG_MIN_SCORE", 0.76))
    if not space_min_score_enabled():
        return default, "global_default"
    if rag_space is None:
        return default, "global_default"

    raw = getattr(rag_space, "min_score", None)
    if raw is None:
        return default, "global_default"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("space_policy.min_score non numerique — defaut global applique")
        return default, "space_value_rejected"
    if value != value or not (MIN_SCORE_FLOOR <= value <= MIN_SCORE_CEILING):
        # `value != value` capte NaN, qui échappe à toute comparaison de bornes.
        logger.warning(
            "space_policy.min_score hors bornes [%s, %s] — defaut global applique",
            MIN_SCORE_FLOOR, MIN_SCORE_CEILING,
        )
        return default, "space_value_rejected"
    # Un espace qui pose EXPLICITEMENT la valeur du défaut n'est pas « sans avis » :
    # un opérateur a tranché, et un futur changement du défaut global ne bougera pas
    # cet espace. La distinction est donc conservée dans la provenance.
    if value == default:
        return value, "space_override_equal_default"
    return value, "space_override"


def resolve_min_score(rag_space: Any) -> float:
    """Seuil applicable à cet espace. Retombe TOUJOURS sur le défaut global.

    Le défaut global est renvoyé quand : le flag est fermé, l'espace est absent,
    l'espace ne porte pas de valeur, ou la valeur portée est inexploitable. Un réglage
    aberrant ne doit jamais rendre un espace muet ni, à l'inverse, désactiver
    silencieusement le garde-fou.
    """
    return _resolve_with_source(rag_space)[0]


def min_score_audit(rag_space: Any, resolved: float | None = None) -> dict:
    """Observabilité anonyme : d'où vient le seuil effectivement appliqué.

    Ne contient ni identifiant d'espace, ni nom, ni question — seulement la provenance
    et la valeur, qui sont des paramètres de configuration et non des données.

    `resolved` n'est plus qu'informatif : la provenance vient de la même décision que
    la valeur, jamais d'une re-dérivation.
    """
    value, source = _resolve_with_source(rag_space)
    return {
        "min_score_source": source,
        "min_score_applied": resolved if resolved is not None else value,
        "min_score_global_default": float(getattr(settings, "RAG_MIN_SCORE", 0.76)),
    }
