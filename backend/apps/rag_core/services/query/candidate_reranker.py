"""Reranker — étape de reranking des candidats (Lot P1-B2).

Responsabilité UNIQUE : décider d'activer le reranking, préparer le pool, appeler le
reranker (INJECTÉ), gérer son repli, et PRÉSERVER l'ordre autoritaire du reranker. Ne
refait NI le retrieval, NI le prompt, NI l'answerability.

INVARIANT ABSOLU : l'ordre retourné par le reranker fait autorité. Aucune étape
postérieure ne re-trie ; la déduplication préserve la première occurrence
(``dedupe_preserving_order``).

Code déplacé VERBATIM depuis ``orchestrator.query_rag`` (bloc « 4) Rerank ») : aucun
changement de pool, de take, de fallback ni d'ordre.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .contracts import QueryExecutionContext, RankedCandidateSet
from .hygiene import dedupe_preserving_order
from .settings import setting_int

#: Budgets du mode « news ». Ce mode est INATTEIGNABLE : il est conditionné par
#: `NEWS_HEURISTIC_ENABLED` (constante False dans l'orchestrateur), et ces deux
#: valeurs ne sont donc jamais lues. Elles restent en constantes plutôt qu'en
#: réglages parce qu'un réglage sur une branche morte est une promesse fausse :
#: le poser ne produirait aucun effet observable.
NEWS_RERANK_POOL_K = 0
NEWS_RERANK_TAKE = 0
from .trace import LEG_RERANK, RetrievalLegStatus

logger = logging.getLogger(__name__)


def rank_candidates(
    ctx: QueryExecutionContext,
    *,
    chunks: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    rerank_fn: Callable[..., tuple[list[dict[str, Any]], Any]],
) -> tuple[list[dict[str, Any]], Any]:
    """Rerank (si applicable) et renvoie ``(selected, rerank_info)``. Renseigne ``ctx.ranked``.

    ``chunks`` = pool candidat (post-fusion/diversification) ; ``selected`` = sélection
    retrieval de repli. Valeurs de politique/mode lues dans les DTO threadés.
    """
    policy = ctx.policy
    prepared = ctx.prepared
    trace = ctx.trace
    assert policy is not None and prepared is not None

    top_k = policy.top_k
    inventory_mode = prepared.inventory
    explanatory_mode = prepared.explanatory
    news_mode = prepared.news
    q_search_primary = prepared.primary_retrieval_query

    # 4) Rerank
    rerank_info = None  # observabilité (B4) : trace persistée dans l'audit
    if policy.rerank_enabled and len(chunks) >= policy.rerank_trigger_min_candidates:
        # Surcharges de budget : `max(courant, surcharge)`. Le défaut du registre
        # vaut 0 — « aucune surcharge » — ce qui reproduit exactement le comportement
        # d'avant, où le défaut d'appel était la valeur courante elle-même.
        rerank_pool_k = max(top_k, setting_int("RAG_RERANK_POOL_K"))
        if inventory_mode:
            rerank_pool_k = max(rerank_pool_k, setting_int("RAG_INVENTORY_RERANK_POOL_K"))
        if explanatory_mode:
            rerank_pool_k = max(rerank_pool_k, setting_int("RAG_EXPLAIN_RERANK_POOL_K"))
        if news_mode:
            rerank_pool_k = max(rerank_pool_k, NEWS_RERANK_POOL_K)

        pool = chunks[:rerank_pool_k]
        reranked, rerank_info = rerank_fn(question=q_search_primary, chunks=pool)

        # L'ordre du reranker fait AUTORITÉ jusqu'à la troncature : le nettoyage qui
        # suit retire les invalides et les doublons SANS réordonner.
        #
        # Ici vivait un défaut fonctionnel : `clean_candidates(reranked)` retriait par
        # `score` — le score de retrieval ANTÉRIEUR — et écrasait le classement du
        # reranker juste avant `reranked[:rerank_take]`. Le reranker tournait, mais son
        # résultat n'atteignait jamais l'utilisateur. Pire, appelé sans `min_score`, il
        # comparait un `ts_rank` (~0,06) au seuil VECTORIEL `RAG_MIN_SCORE` (0,76) sur
        # un pool mono-modalité : la liste entière était anéantie, et le repli
        # `or selected` ci-dessous restaurait l'ordre retrieval en silence.
        n_before_clean = len(reranked)
        reranked = dedupe_preserving_order(reranked)

        rerank_take = int(policy.rerank_top_k)
        if inventory_mode:
            rerank_take = max(rerank_take, setting_int("RAG_INVENTORY_RERANK_TAKE"))
        if explanatory_mode:
            rerank_take = max(rerank_take, setting_int("RAG_EXPLAIN_RERANK_TAKE"))
        if news_mode:
            rerank_take = max(rerank_take, NEWS_RERANK_TAKE)

        # Repli : si le rerank ne rend rien d'exploitable, on garde la sélection
        # retrieval plutôt que de répondre sans source. Il est journalisé — c'est
        # précisément ce repli, muet jusqu'ici, qui a masqué le défaut ci-dessus
        # pendant toute la vie du reranker.
        rerank_applied = bool(reranked[:rerank_take])
        if not rerank_applied:
            logger.warning(
                "retrieval.rerank fallback: aucun candidat après nettoyage "
                "(pool=%d, rerank_out=%d, take=%d) -> ordre retrieval conservé",
                len(pool), n_before_clean, rerank_take,
            )
        else:
            logger.info(
                "retrieval.rerank applied pool=%d rerank_out=%d post_clean=%d take=%d",
                len(pool), n_before_clean, len(reranked), rerank_take,
            )
        selected = reranked[:rerank_take] or selected

    # Leg rerank : réutilise l'observabilité EXISTANTE (`RerankInfo.fallback_used`),
    # sans seconde doctrine. Un fallback (cross-encoder KO -> stub) est une
    # dégradation de qualité -> `degraded`. En config par défaut (provider `stub`
    # demandé), `fallback_used=False` -> `succeeded`, nominal inchangé.
    if rerank_info is not None:
        _rerank_fallback = bool(getattr(rerank_info, "fallback_used", False))
        trace.record_leg(
            LEG_RERANK,
            RetrievalLegStatus.DEGRADED if _rerank_fallback else RetrievalLegStatus.SUCCEEDED,
            candidate_count=len(selected),
            fallback_used=_rerank_fallback,
            error_kind="rerank_fallback" if _rerank_fallback else None,
        )
        if _rerank_fallback:
            trace.add_fallback_reason("rerank_fallback")
    else:
        trace.record_leg(LEG_RERANK, RetrievalLegStatus.NOT_REQUESTED)

    # --- Contrat CANDIDATS ORDONNÉS (P1-B1, consommé en P1-B2) ------------------
    # L'ORDRE de `selected` EST le contrat de ranking (aucun re-tri en aval). Références.
    ctx.ranked = RankedCandidateSet.from_selected(
        selected,
        rerank_applied=rerank_info is not None,
        fallback_used=bool(getattr(rerank_info, "fallback_used", False)),
    )
    return selected, rerank_info
