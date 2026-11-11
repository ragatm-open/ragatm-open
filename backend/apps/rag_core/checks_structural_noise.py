"""System check — facteur de pénalité structurelle dans ses bornes.

Le facteur ne s'applique qu'en multiplication d'une clé de classement : une valeur
aberrante ne provoque aucune erreur, elle déforme silencieusement l'ordre des candidats.
On la refuse donc au démarrage plutôt que de la découvrir en production.

Bornes : [0,50 ; 1,00]. En deçà de 0,50 la « dévalorisation » devient une exclusion de
fait — hors doctrine de ce lot, qui est non destructif. 0 est interdit.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning

from apps.rag_core.services.query.passage_shape import (
    MAX_PENALTY_FACTOR,
    MIN_PENALTY_FACTOR,
)

ID_INVALID = "rag_core.E006"
ID_INERT = "rag_core.W002"


def check_structural_noise_penalty(app_configs, **kwargs):
    """Refuse un facteur hors bornes ou illisible ; signale un réglage inerte."""
    issues: list = []
    raw = getattr(settings, "RAG_STRUCTURAL_NOISE_PENALTY_FACTOR", None)
    enabled = bool(getattr(settings, "RAG_STRUCTURAL_NOISE_PENALTY_ENABLED", False))

    if raw is None:
        return issues

    try:
        factor = float(raw)
    except (TypeError, ValueError):
        issues.append(
            Error(
                f"RAG_STRUCTURAL_NOISE_PENALTY_FACTOR={raw!r} n'est pas un nombre.",
                hint=f"Posez une valeur entre {MIN_PENALTY_FACTOR} et {MAX_PENALTY_FACTOR}.",
                id=ID_INVALID,
            )
        )
        return issues

    if factor != factor or not (MIN_PENALTY_FACTOR <= factor <= MAX_PENALTY_FACTOR):
        issues.append(
            Error(
                f"RAG_STRUCTURAL_NOISE_PENALTY_FACTOR={factor} est hors bornes "
                f"[{MIN_PENALTY_FACTOR} ; {MAX_PENALTY_FACTOR}].",
                hint=(
                    "En deçà du plancher, la dévalorisation devient une exclusion de fait : "
                    "ce n'est pas la doctrine de ce mécanisme. 1.00 = neutre (rollback)."
                ),
                id=ID_INVALID,
            )
        )
        return issues

    if enabled and factor == MAX_PENALTY_FACTOR:
        issues.append(
            CheckWarning(
                "RAG_STRUCTURAL_NOISE_PENALTY_ENABLED=1 avec un facteur de 1.00 : "
                "le mécanisme est actif mais neutre.",
                hint="Posez un facteur < 1.00 (0.75 recommandé), ou désactivez le flag.",
                id=ID_INERT,
            )
        )
    return issues
