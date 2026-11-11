"""Policy d'admission `balanced-v1` — module PUR, évalué en OBSERVATION seulement.

Aucune dépendance Django, aucun accès base, aucun appel réseau, aucun appel modèle.
Il ne reçoit qu'une structure déjà produite par le reranking et rend une décision.

## Ce que cette policy fait, et ce qu'elle ne fait pas

Elle **calcule** `ALLOW` / `ABSTAIN` / `UNCERTAIN` à partir du score cross-encoder du
meilleur passage. Elle **ne décide rien** : aucune réponse utilisateur, aucun appel LLM,
aucun classement, aucune source ne dépend de sa sortie. Avec le drapeau ON ou OFF, la
réponse servie est identique — c'est la propriété que les tests verrouillent.

## D'où viennent les deux nombres

Ils ne sont pas choisis ici. Ils sortent du lot de conception
(`backend/benchmarks/calibration/admission-policy-design/`), et ils sont **ancrés sur la
distribution des NÉGATIFS**, pas des positifs :

    ABSTAIN  si top1_score <= -1.92    (médiane des négatifs du jeu d'ajustement)
    ALLOW    si top1_score >= +1.60    (p95 des négatifs)
    UNCERTAIN entre les deux

La raison de cet ancrage est mesurée, pas esthétique : la queue basse des positifs n'est
pas stable d'un jeu à l'autre — minimum +1,96 en development, −0,02 en calibration,
**−4,65** sur le holdout — alors que les négatifs le sont (médiane −1,92 / −2,32 / −1,34).
Un seuil calé sur un quantile bas des positifs aurait valu +2,28 et aurait rejeté
massivement ailleurs.

## Pourquoi les seuils ne sont pas configurables

Une policy dont les seuils se règlent par variable d'environnement n'est pas une policy
versionnée : c'est un réglage anonyme. Même doctrine que `MAX_QUESTION_CHARS`. Déplacer
`-1.92` ou `+1.60` **exige une nouvelle `POLICY_VERSION`**, donc un lot et une trace.
Seule l'ACTIVATION du calcul est un drapeau (`RAG_ADMISSION_SHADOW_ENABLED`).

## Ce qui n'est pas évaluable

Le reranker tourne en `fail_open_rrf` : en cas de panne, il rend l'ordre RRF. Les scores
d'une sortie RRF ne sont **pas** des logits cross-encoder — les comparer à `-1.92` n'aurait
aucun sens. Ces cas rendent `NOT_EVALUATED` avec un motif nommé, jamais `ALLOW` ni
`ABSTAIN` par défaut. Fabriquer un `top1_score` serait pire qu'une absence de décision.
"""
from __future__ import annotations

from typing import Any, Mapping

#: Toute modification d'un seuil ou de la règle DOIT changer cette version.
POLICY_VERSION = "balanced-v1"

#: Signal évalué — le score cross-encoder du passage classé premier après reranking.
SIGNAL = "reranker_top1_score"

ABSTAIN_LTE = -1.92
ALLOW_GTE = 1.60

ALLOW = "allow"
ABSTAIN = "abstain"
UNCERTAIN = "uncertain"
NOT_EVALUATED = "not_evaluated"

#: Motifs de non-évaluation. Chacun décrit un état RÉEL du pipeline, jamais un défaut
#: de la policy elle-même.
REASON_DISABLED = "shadow_disabled"
REASON_RERANKER_UNAVAILABLE = "reranker_unavailable"
REASON_RERANKER_FALLBACK = "reranker_fallback"
REASON_NO_CANDIDATES = "no_candidates"
REASON_NO_SCORE = "no_top1_score"
REASON_EVALUATED = "evaluated"

__all__ = [
    "POLICY_VERSION", "SIGNAL", "ABSTAIN_LTE", "ALLOW_GTE",
    "ALLOW", "ABSTAIN", "UNCERTAIN", "NOT_EVALUATED",
    "REASON_DISABLED", "REASON_RERANKER_UNAVAILABLE", "REASON_RERANKER_FALLBACK",
    "REASON_NO_CANDIDATES", "REASON_NO_SCORE", "REASON_EVALUATED",
    "decide", "evaluate", "policy_descriptor",
]


def policy_descriptor() -> dict[str, Any]:
    """Représentation canonique de la policy — ce qui part dans la trace."""
    return {
        "policy_version": POLICY_VERSION,
        "status": "shadow",
        "signal": SIGNAL,
        "abstain_lte": ABSTAIN_LTE,
        "allow_gte": ALLOW_GTE,
        "source_lot": "admission-policy-design",
    }


def decide(top1_score: float) -> str:
    """La règle, et rien d'autre. Bornes INCLUSIVES des deux côtés.

    `<=` et `>=` : un score exactement égal à un seuil tombe du côté décidé, pas dans
    l'incertain. Ce choix est arbitraire mais il doit être FIXE et testé — une borne
    dont on ne sait pas de quel côté elle penche produit des décisions irreproductibles
    à la valeur exacte du seuil.
    """
    if top1_score <= ABSTAIN_LTE:
        return ABSTAIN
    if top1_score >= ALLOW_GTE:
        return ALLOW
    return UNCERTAIN


def _top1_score(info: Mapping[str, Any]) -> float | None:
    """Score du candidat classé premier APRÈS reranking.

    Lu dans `movements`, jamais recalculé : le rang 1 est celui que le reranker a
    désigné, pas celui que l'ordre de la liste suggère.
    """
    meilleur = None
    for m in info.get("movements") or ():
        if not isinstance(m, Mapping):
            continue
        if m.get("after_rank") == 1:
            score = m.get("score")
            return float(score) if isinstance(score, (int, float)) else None
        rang = m.get("after_rank")
        score = m.get("score")
        if isinstance(rang, int) and isinstance(score, (int, float)):
            if meilleur is None or rang < meilleur[0]:
                meilleur = (rang, float(score))
    return None if meilleur is None else meilleur[1]


def evaluate(
    rerank_info: Mapping[str, Any] | None,
    *,
    enabled: bool,
    candidate_count: int | None = None,
) -> dict[str, Any]:
    """Évalue la policy en OBSERVATION. Ne modifie rien, ne rappelle rien.

    `rerank_info` est la structure déjà rendue par `rerank_gateway.rerank_chunks` :
    aucun appel supplémentaire n'est fait, ni au reranker, ni à la Gateway, ni à un LLM.

    La distinction qui compte est entre `success` et `fallback` : un repli `fail_open_rrf`
    rend un ordre RRF dont les scores ne sont pas des logits cross-encoder. Il est
    explicitement `NOT_EVALUATED`, jamais rangé sous « balanced-v1 evaluated ».
    """
    base = policy_descriptor()
    base["shadow_enabled"] = bool(enabled)

    def _non_evalue(reason: str, **extra: Any) -> dict[str, Any]:
        return {**base, "decision": NOT_EVALUATED, "reason": reason,
                "top1_score": None, **extra}

    if not enabled:
        return _non_evalue(REASON_DISABLED)

    info = rerank_info if isinstance(rerank_info, Mapping) else {}
    if not info:
        return _non_evalue(REASON_RERANKER_UNAVAILABLE)

    if info.get("fallback"):
        return _non_evalue(REASON_RERANKER_FALLBACK,
                           reranker_status="fallback",
                           fallback_used=True)
    if not info.get("success"):
        return _non_evalue(REASON_RERANKER_UNAVAILABLE,
                           reranker_status="failed",
                           fallback_used=bool(info.get("fallback")))

    if candidate_count == 0 or not (info.get("movements") or ()):
        return _non_evalue(REASON_NO_CANDIDATES,
                           reranker_status="succeeded", fallback_used=False)

    score = _top1_score(info)
    if score is None:
        return _non_evalue(REASON_NO_SCORE,
                           reranker_status="succeeded", fallback_used=False)

    return {
        **base,
        "decision": decide(score),
        "reason": REASON_EVALUATED,
        "top1_score": round(float(score), 6),
        "reranker_status": "succeeded",
        "fallback_used": False,
        "model_revision": info.get("model_revision") or None,
        "model_logical": info.get("model_logical") or None,
    }
