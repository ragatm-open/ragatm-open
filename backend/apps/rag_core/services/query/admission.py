"""Pont entre le pipeline de requête et la politique d'admission (lot 4).

Le décideur vit dans ``domain/retrieval_admission`` — module pur, sans Django. Ce
module-ci fait les trois choses que le domaine ne peut pas faire : lire la
configuration, journaliser, compter.

## Compatibilité

``strict_refusal`` reste un booléen dans l'orchestrateur. La politique ``legacy``
reproduit ``should_answer`` à l'octet près, et c'est le défaut : tant que
``RAG_ADMISSION_POLICY`` n'est pas changée, aucune requête ne décide autrement
qu'avant.

## Journalisation

Un événement structuré par décision, sans texte de question, sans chunk, sans
vecteur. Les compteurs sont tenus en processus — RagATM n'embarque aucun backend de
métriques, et en inventer un sortirait du périmètre.
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from apps.rag_core.domain.retrieval_admission import (
    ACCEPT,
    POLICY_LEGACY,
    REJECT,
    TEXT_POLICY_INDEPENDENT,
    UNCERTAIN,
    AdmissionDecision,
    AdmissionThresholds,
    build_evidence,
    decide,
)

logger = logging.getLogger("rag.retrieval.admission")

#: Compteurs en processus. Cardinalité BORNÉE par construction : `decision` a trois
#: valeurs, `policy` quatre, `mode` quatre. Aucune dimension libre (ni espace, ni
#: question, ni document) — ce sont elles qui font exploser une série temporelle.
_COUNTERS: dict[str, int] = {}


def _bump(name: str, value: int = 1) -> None:
    _COUNTERS[name] = _COUNTERS.get(name, 0) + int(value)


def counters_snapshot() -> dict[str, int]:
    return dict(_COUNTERS)


def reset_counters() -> None:
    """Remise à zéro — réservée aux tests."""
    _COUNTERS.clear()


def resolve_thresholds(rag_space: Any = None, base_policy: Any = None) -> AdmissionThresholds:
    """Seuils EFFECTIFS de CETTE requête.

    ``base_policy`` (le ``RagPolicy`` déjà résolu par l'orchestrateur) est
    PRIORITAIRE, et ce n'est pas un détail : l'orchestrateur applique des budgets
    par mode — l'explicatif abaisse `min_score`, l'inventaire élargit les seuils, le
    seuil par espace peut primer. Relire les settings ici écraserait tout cela et
    ferait décider l'admission sur des seuils que la requête n'utilise pas.

    Le lot 4 n'ajoute donc que SES paramètres (politique, marge, incertitude,
    traitement du lexical) par-dessus une politique déjà résolue.
    """
    def _from(name: str, fallback):
        v = getattr(base_policy, name, None) if base_policy is not None else None
        return fallback if v is None else v

    min_score = float(_from("min_score", getattr(settings, "RAG_MIN_SCORE", 0.76)))
    if base_policy is None and rag_space is not None:
        try:
            from apps.rag_core.services.query.space_policy import resolve_min_score

            min_score = float(resolve_min_score(rag_space))
        except Exception:  # pragma: no cover - repli sur le défaut global
            logger.debug("space_policy indisponible, seuil global conservé")
    return AdmissionThresholds(
        policy=str(getattr(settings, "RAG_ADMISSION_POLICY", POLICY_LEGACY) or POLICY_LEGACY),
        min_sources=int(_from("min_sources", getattr(settings, "RAG_MIN_SOURCES", 1))),
        min_score=min_score,
        min_margin=float(getattr(settings, "RAG_MIN_MARGIN", 0.0)),
        uncertain_score=float(getattr(settings, "RAG_UNCERTAIN_SCORE", 0.0)),
        min_text_rank=float(_from("min_text_rank", getattr(settings, "RAG_MIN_TEXT_RANK", 0.0))),
        text_policy=str(getattr(settings, "RAG_TEXT_ADMISSION_POLICY", TEXT_POLICY_INDEPENDENT)
                        or TEXT_POLICY_INDEPENDENT),
    )


def expected_family_for(rag_space: Any) -> str:
    """Famille de pipeline ATTENDUE d'un espace, ou "" s'il n'est pas gouverné.

    Rendre "" plutôt que de deviner : `RetrievalEvidence.pipeline_is_consistent`
    n'affirme rien sans famille attendue. Un espace historique ne doit pas être
    refusé au motif qu'il ne porte pas d'identité de pipeline.
    """
    try:
        from apps.llm_router.embeddings import space_is_governed

        if rag_space is None or not space_is_governed(rag_space):
            return ""
        from apps.rag_core.services.embedding_pipeline_coverage import expected_pipeline_family

        return expected_pipeline_family(rag_space) or ""
    except Exception:  # pragma: no cover - jamais bloquant pour une requête
        logger.debug("famille de pipeline attendue indéterminable")
        return ""


def admit(
    chunks: list[dict[str, Any]] | None,
    *,
    rag_space: Any = None,
    base_policy: Any = None,
    expected_pipeline_family: str = "",
    request_id: str = "",
    latency_ms: float = 0.0,
) -> AdmissionDecision:
    """Décide, journalise et compte. Ne modifie AUCUN candidat.

    Une configuration invalide n'est pas une raison d'accepter : elle retombe sur
    ``legacy`` et le SIGNALE. Le system check ``rag_core.E012`` l'aurait déjà refusée
    au démarrage ; ce repli couvre un override runtime arrivé après.
    """
    try:
        thresholds = resolve_thresholds(rag_space, base_policy=base_policy)
    except (ValueError, TypeError) as exc:
        _bump("rag_retrieval_admission_config_error_total")
        logger.error(
            "rag_retrieval_admission_config_invalid",
            extra={"error": str(exc), "request_id": request_id},
        )
        thresholds = AdmissionThresholds(policy=POLICY_LEGACY)

    evidence = build_evidence(chunks, expected_pipeline_family=expected_pipeline_family)
    decision = decide(evidence, thresholds)

    _bump(f"rag_retrieval_admission_total|decision={decision.status}"
          f"|policy={thresholds.policy}|mode={evidence.retrieval_mode}")
    if evidence.source_count == 0:
        _bump("rag_retrieval_empty_total")
    if decision.reason == "foreign_pipeline":
        _bump("rag_retrieval_foreign_pipeline_total")
    if decision.fallback_allowed:
        _bump("rag_retrieval_fts_fallback_total")

    logger.info(
        "rag_retrieval_admission",
        extra={
            "event": "rag_retrieval_admission",
            "request_id": request_id,
            **decision.to_log_fields(),
            **evidence.to_log_fields(),
            "latency_ms": int(latency_ms),
        },
    )
    return decision


__all__ = [
    "ACCEPT", "UNCERTAIN", "REJECT",
    "admit", "counters_snapshot", "expected_family_for", "reset_counters",
    "resolve_thresholds",
]
