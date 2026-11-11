"""System check — cohérence des budgets analytiques (explicatif / comparatif).

Ces réglages ne sont appliqués que par `max()` (tailles) ou `min()` (seuils) : une
valeur mal choisie n'échoue pas, elle est simplement **inerte**. C'est exactement le
défaut que l'évaluation a mis au jour — une configuration annoncée mais sans effet.
On la rend donc visible au démarrage plutôt qu'invisible en production.

Aucune valeur sensible n'est affichée : ce sont des tailles de top-k et des seuils.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning

ID_INVALID = "rag_core.E005"
ID_INERT = "rag_core.W001"


def _get(name: str, default=None):
    return getattr(settings, name, default)


def check_analytical_budgets(app_configs, **kwargs):
    """Refuse une taille non positive ; signale un réglage analytique sans effet."""
    issues: list = []

    top_k = int(_get("RAG_TOP_K", 8) or 0)
    vector_top_k = int(_get("RAG_VECTOR_TOP_K", 8) or 0)
    explain_top_k = int(_get("RAG_EXPLAIN_TOP_K", top_k) or 0)
    explain_vector_top_k = int(_get("RAG_EXPLAIN_VECTOR_TOP_K", vector_top_k) or 0)

    for name, value in (
        ("RAG_EXPLAIN_TOP_K", explain_top_k),
        ("RAG_EXPLAIN_VECTOR_TOP_K", explain_vector_top_k),
    ):
        if value <= 0:
            issues.append(
                Error(
                    f"{name}={value} : une taille de top-k doit être strictement positive.",
                    hint="Retirez le réglage pour hériter du budget standard, ou posez une valeur ≥ 1.",
                    id=ID_INVALID,
                )
            )

    # Inertie : l'orchestrateur ne fait qu'ÉLARGIR (max) — une valeur plus petite que le
    # budget standard ne réduit rien, elle ne fait rien.
    if 0 < explain_top_k < top_k:
        issues.append(
            CheckWarning(
                f"RAG_EXPLAIN_TOP_K={explain_top_k} est INERTE : il vaut moins que "
                f"RAG_TOP_K={top_k}, et l'orchestrateur applique "
                f"max(RAG_TOP_K, RAG_EXPLAIN_TOP_K) — le mode analytique ne peut "
                "qu'élargir le budget, jamais le réduire.",
                hint=(
                    "Deux issues, toutes deux légitimes : (1) RETIRER le réglage, si "
                    "le mode explicatif doit simplement hériter du budget standard ; "
                    f"(2) poser une valeur ≥ {top_k}, si l'on veut réellement élargir — "
                    "auquel cas la valeur se calibre par benchmark, pas au jugé. Le "
                    "laisser plus petit entretient l'illusion d'un budget réduit pour "
                    "les questions explicatives, qui n'existe pas."
                ),
                id=ID_INERT,
            )
        )
    if 0 < explain_vector_top_k < vector_top_k:
        issues.append(
            CheckWarning(
                f"RAG_EXPLAIN_VECTOR_TOP_K={explain_vector_top_k} est INERTE : il vaut "
                f"moins que RAG_VECTOR_TOP_K={vector_top_k}, et l'orchestrateur applique "
                "max(standard, analytique).",
                hint=(
                    f"Retirez le réglage, ou posez une valeur ≥ {vector_top_k}. Un "
                    "budget vectoriel analytique plus étroit que le standard n'est "
                    "pas appliqué : il est ignoré."
                ),
                id=ID_INERT,
            )
        )

    # Seuils : l'orchestrateur ne fait qu'ABAISSER (min) — une valeur plus haute est inerte.
    explain_min_score = _get("RAG_EXPLAIN_MIN_SCORE", None)
    min_score = float(_get("RAG_MIN_SCORE", 0.0) or 0.0)
    if explain_min_score is not None and float(explain_min_score) > min_score:
        issues.append(
            CheckWarning(
                f"RAG_EXPLAIN_MIN_SCORE={explain_min_score} est supérieur à "
                f"RAG_MIN_SCORE={min_score} : sans effet (le mode analytique ne peut "
                "qu'abaisser le seuil).",
                hint="Posez une valeur ≤ RAG_MIN_SCORE, ou retirez le réglage.",
                id=ID_INERT,
            )
        )

    explain_min_text_rank = _get("RAG_EXPLAIN_MIN_TEXT_RANK", None)
    min_text_rank = float(_get("RAG_MIN_TEXT_RANK", 0.0) or 0.0)
    if explain_min_text_rank is not None and float(explain_min_text_rank) > min_text_rank:
        issues.append(
            CheckWarning(
                f"RAG_EXPLAIN_MIN_TEXT_RANK={explain_min_text_rank} est supérieur à "
                f"RAG_MIN_TEXT_RANK={min_text_rank} : sans effet.",
                hint="Posez une valeur ≤ RAG_MIN_TEXT_RANK, ou retirez le réglage.",
                id=ID_INERT,
            )
        )

    return issues
