"""Point d'entrée UNIQUE appelé par le pipeline de retrieval.

Toute la logique de capture vit dans ``apps.feedback`` ; ``apps.rag_core`` ne
connaît que cette fonction, l'appelle dans un ``try/except`` et n'en attend
aucune valeur. Ce sens de dépendance est délibéré : le retrieval ne doit pas
dépendre du feedback, ni dans son code, ni dans son comportement.

Garanties tenues ici, et vérifiées par les tests :

- drapeau à 0     -> retour immédiat, aucune allocation, aucune I/O ;
- toute exception -> avalée, tracée par un code d'erreur, jamais propagée ;
- aucune valeur de retour n'influence le pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def capture_retrieval_trace(
    *,
    log_id,
    pool: list[dict[str, Any]] | None,
    selected_pre_rerank: list[dict[str, Any]] | None,
    selected_post_rerank: list[dict[str, Any]] | None,
    selected_final: list[dict[str, Any]] | None,
    top_k: int,
    rerank_applied: bool,
    diversity_applied: bool,
    answerability_policy: str,
    thresholds: dict[str, Any] | None = None,
    budgets: dict[str, Any] | None = None,
    embedding_model: str = "",
    structural_penalised_ids: set[str] | None = None,
    created_at: str = "",
    sink=None,
) -> bool:
    """Capture la trace des candidats. Ne lève jamais.

    Deux destinations possibles, indépendantes :

    - le **tampon** — production, sous drapeau, pour les futurs signalements ;
    - un ``sink`` — un appelant qui veut la trace ICI ET MAINTENANT sans rien
      persister. C'est ce que fait le replay : il a besoin du pool de candidats,
      or celui-ci n'est visible qu'ici. Sans ce point d'observation, un replay ne
      pourrait comparer que les sources RETENUES, et resterait muet sur la seule
      question qui compte — le bon chunk était-il seulement candidat ?
    """
    try:
        from . import buffer, capture

        to_buffer = bool(log_id) and buffer.is_enabled()
        if not to_buffer and sink is None:
            return False

        candidate_trace = capture.build_candidate_trace(
            pool=pool,
            selected_pre_rerank=selected_pre_rerank,
            selected_post_rerank=selected_post_rerank,
            selected_final=selected_final,
            top_k=top_k,
            rerank_applied=rerank_applied,
            diversity_applied=diversity_applied,
            structural_penalised_ids=structural_penalised_ids,
            max_candidates=buffer.top_k(),
        )
        selection = capture.build_selection(
            selected_final=selected_final,
            answerability_policy=answerability_policy,
            thresholds=thresholds,
            budgets=budgets,
        )
        payload = capture.build_trace_payload(
            log_id=log_id or "",
            created_at=created_at,
            embedding_model=embedding_model,
            candidate_trace=candidate_trace,
            selection=selection,
        )

        if sink is not None:
            try:
                sink(payload)
            except Exception as exc:  # noqa: BLE001 - un observateur ne casse rien
                logger.warning(
                    "feedback_trace_sink_failed error_code=%s", type(exc).__name__
                )

        return buffer.write_trace(log_id, payload) if to_buffer else True
    except Exception as exc:  # noqa: BLE001 - l'observabilité ne casse pas le chat
        logger.warning(
            "feedback_buffer_capture_failed log_id=%s error_code=%s",
            log_id,
            type(exc).__name__,
        )
        return False
