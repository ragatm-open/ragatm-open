"""System checks de la politique d'admission du retrieval (lot 4).

Une politique d'admission mal configurée ne plante pas : elle **décide autrement**.
Un `RAG_UNCERTAIN_SCORE` au-dessus de `RAG_MIN_SCORE` produirait une zone
d'incertitude au-dessus du seuil d'acceptation — logiquement vide, donc silencieuse.
Un nom de politique mal orthographié retomberait sur un défaut sans que personne ne
le voie.

Ces checks refusent au DÉMARRAGE ce que le runtime ne saurait pas signaler.
"""
from __future__ import annotations

from django.core.checks import Error, Warning as CheckWarning, register


@register()
def check_admission_policy(app_configs, **kwargs):
    """`rag_core.E010`..`E013` — cohérence de la politique d'admission."""
    from django.conf import settings

    from apps.rag_core.domain.retrieval_admission import (
        ADMISSION_POLICIES,
        POLICY_LEGACY,
        POLICY_TIERED,
        TEXT_ADMISSION_POLICIES,
        AdmissionThresholds,
    )

    issues: list = []
    policy = str(getattr(settings, "RAG_ADMISSION_POLICY", POLICY_LEGACY) or "").strip()
    text_policy = str(getattr(settings, "RAG_TEXT_ADMISSION_POLICY", "") or "").strip()

    if policy not in ADMISSION_POLICIES:
        issues.append(Error(
            f"RAG_ADMISSION_POLICY={policy!r} est inconnue.",
            hint=f"Valeurs admises : {', '.join(sorted(ADMISSION_POLICIES))}.",
            id="rag_core.E010",
        ))
    if text_policy not in TEXT_ADMISSION_POLICIES:
        issues.append(Error(
            f"RAG_TEXT_ADMISSION_POLICY={text_policy!r} est inconnue.",
            hint=f"Valeurs admises : {', '.join(sorted(TEXT_ADMISSION_POLICIES))}.",
            id="rag_core.E011",
        ))
    if issues:
        # Sans politique valide, les contrôles de seuils n'ont pas d'objet.
        return issues

    try:
        AdmissionThresholds(
            policy=policy,
            min_sources=int(getattr(settings, "RAG_MIN_SOURCES", 1)),
            min_score=float(getattr(settings, "RAG_MIN_SCORE", 0.76)),
            min_margin=float(getattr(settings, "RAG_MIN_MARGIN", 0.0)),
            uncertain_score=float(getattr(settings, "RAG_UNCERTAIN_SCORE", 0.0)),
            min_text_rank=float(getattr(settings, "RAG_MIN_TEXT_RANK", 0.0)),
            text_policy=text_policy,
        )
    except (ValueError, TypeError) as exc:
        return [Error(
            f"Configuration d'admission incohérente : {exc}",
            hint="Corriger RAG_MIN_SCORE / RAG_MIN_MARGIN / RAG_UNCERTAIN_SCORE / RAG_MIN_SOURCES.",
            id="rag_core.E012",
        )]

    # Avertissements : configurations VALIDES mais qui ne font pas ce qu'on croit.
    if policy == POLICY_TIERED and float(getattr(settings, "RAG_UNCERTAIN_SCORE", 0.0)) <= 0.0:
        issues.append(CheckWarning(
            "RAG_ADMISSION_POLICY=tiered avec RAG_UNCERTAIN_SCORE=0 : la zone "
            "UNCERTAIN ne rejettera jamais rien sur le score seul.",
            hint="Poser RAG_UNCERTAIN_SCORE sous RAG_MIN_SCORE, ou choisir score_and_margin.",
            id="rag_core.W010",
        ))
    if policy in ("score_and_margin", POLICY_TIERED) and float(getattr(settings, "RAG_MIN_MARGIN", 0.0)) <= 0.0:
        issues.append(CheckWarning(
            f"RAG_ADMISSION_POLICY={policy} avec RAG_MIN_MARGIN=0 : la séparation "
            "top-1/top-2 n'est pas exigée, la politique se comporte comme score_only.",
            hint="Poser RAG_MIN_MARGIN > 0, ou choisir score_only si c'est l'intention.",
            id="rag_core.W011",
        ))
    return issues
