"""Métriques de qualité RAG — fonctions pures, sans Django ni I/O.

Toutes les fonctions opèrent sur des listes d'identifiants de documents déjà
ordonnées par rang (rang 1 = meilleur). Elles sont déterministes et testables
unitairement sans base de données.

Conventions :
- ``retrieved`` : identifiants des documents retournés par le pipeline, dans
  l'ordre de classement, dédupliqués (un document n'apparaît qu'une fois) ;
- ``expected``  : identifiants des documents attendus (vérité terrain).
"""
from __future__ import annotations

from typing import Iterable, Sequence


def dedup_preserve_order(items: Iterable[str]) -> list[str]:
    """Déduplique en conservant l'ordre du premier passage (rang document)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it is None:
            continue
        key = str(it)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def recall_at_k(retrieved: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Fraction des documents attendus présents dans le top-k.

    recall@k = |expected ∩ retrieved[:k]| / |expected|.
    Si ``expected`` est vide (cas no-answer), la notion de recall n'a pas de sens :
    on renvoie ``0.0`` et l'appelant doit exclure ces cas de l'agrégat de recall.
    """
    exp = set(str(e) for e in expected)
    if not exp:
        return 0.0
    if k <= 0:
        return 0.0
    topk = set(str(r) for r in retrieved[:k])
    return len(exp & topk) / len(exp)


def recall_at_k_policy(
    retrieved: Sequence[str], expected: Sequence[str], k: int, match_policy: str = "any"
) -> float:
    """recall@k tenant compte de la ``match_policy`` d'un cas v2.

    - ``any`` : 1.0 si AU MOINS un chunk attendu (région pouvant être découpée en
      plusieurs chunks équivalents) apparaît dans le top-k, sinon 0.0. On ne pénalise
      donc pas un chunking qui répartit la même région autrement.
    - ``all`` : fraction des chunks requis présents (``= recall_at_k``) — vaut 1.0
      uniquement si TOUS sont dans le top-k.
    """
    exp = set(str(e) for e in expected)
    if not exp or k <= 0:
        return 0.0
    hit = exp & set(str(r) for r in retrieved[:k])
    if match_policy == "any":
        return 1.0 if hit else 0.0
    return len(hit) / len(exp)


def precision_at_k(retrieved: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Précision@k = |expected ∩ retrieved[:k]| / k (sur les rangs réellement renvoyés)."""
    if k <= 0:
        return 0.0
    exp = set(str(e) for e in expected)
    topk = [str(r) for r in retrieved[:k]]
    if not topk:
        return 0.0
    hits = sum(1 for r in topk if r in exp)
    return hits / len(topk)


def reciprocal_rank(retrieved: Sequence[str], expected: Sequence[str]) -> float:
    """1 / rang du premier document attendu trouvé ; 0.0 si aucun."""
    exp = set(str(e) for e in expected)
    if not exp:
        return 0.0
    for idx, r in enumerate(retrieved, start=1):
        if str(r) in exp:
            return 1.0 / idx
    return 0.0


def first_relevant_rank(retrieved: Sequence[str], expected: Sequence[str]) -> int | None:
    """Rang (1-based) du premier document attendu, ou ``None`` si absent."""
    exp = set(str(e) for e in expected)
    if not exp:
        return None
    for idx, r in enumerate(retrieved, start=1):
        if str(r) in exp:
            return idx
    return None


def no_answer_correct(*, should_answer: bool, status: str) -> bool:
    """Décision répondre/ne-pas-répondre correcte ?

    - should_answer=False : correct si le système refuse (status == ``no_answer``) ;
    - should_answer=True  : correct si le système répond (status == ``ok``).
    Le status ``error`` est toujours considéré incorrect.
    """
    s = (status or "").strip().lower()
    if should_answer:
        return s == "ok"
    return s == "no_answer"


def citation_precision(cited_ranks: Sequence[int], n_sources: int) -> float:
    """Part des citations ``[n]`` qui pointent vers une source réellement fournie.

    Mesure anti-hallucination de référence : une citation ``[5]`` alors que seules
    3 sources existent est invalide. Renvoie ``1.0`` si aucune citation (rien à
    invalider). ``n_sources`` = nombre de sources injectées dans le prompt.
    """
    cites = [int(c) for c in cited_ranks if c is not None]
    if not cites:
        return 1.0
    valid = sum(1 for c in cites if 1 <= c <= n_sources)
    return valid / len(cites)


def answer_contains_score(answer: str, expected_terms: Sequence[str]) -> float:
    """Proxy simple (sans LLM-judge) : part des termes attendus présents dans la réponse.

    Comparaison insensible à la casse et aux accents non gérée ici volontairement
    (les termes attendus doivent être choisis robustes). Renvoie ``1.0`` si aucun
    terme attendu (rien à vérifier).
    """
    terms = [t for t in (expected_terms or []) if str(t).strip()]
    if not terms:
        return 1.0
    hay = (answer or "").lower()
    hits = sum(1 for t in terms if str(t).strip().lower() in hay)
    return hits / len(terms)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Percentile (interpolation linéaire) ; ``None`` si la liste est vide.

    ``p`` ∈ [0, 100]. Implémentation locale pour éviter une dépendance numpy.
    """
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    p = max(0.0, min(100.0, float(p)))
    rank = (p / 100.0) * (len(vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(vals) - 1)
    frac = rank - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac


def mean(values: Sequence[float]) -> float | None:
    """Moyenne arithmétique ; ``None`` si vide."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)
