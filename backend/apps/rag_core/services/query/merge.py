from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


from apps.rag_core.domain.ranking import sort_candidates

#: Modalités de recherche. Le RRF fusionne des RANGS : la modalité ne sert qu'à
#: reconstituer `text_score`/`vector_score`, qui restent le contrat exposé en aval
#: (seuils d'hygiène par modalité, `should_answer`, API).
MODALITY_TEXT = "text"
MODALITY_VECTOR = "vector"

#: Constante de la fusion par rang réciproque (RRF). 60 est la valeur de référence de
#: la littérature : elle amortit l'écart entre les premiers rangs et évite qu'un seul
#: résultat de tête d'une modalité n'écrase toute l'autre.
#:
#: `RAG_RRF_K` a longtemps semblé pouvoir l'écraser : le code lisait
#: `getattr(settings, "RAG_RRF_K", 60)` alors qu'aucun settings ne projetait ce nom.
#: Le réglage n'a donc JAMAIS eu d'effet, et P2.5 avait besoin de mesurer K, pas de
#: le changer en exploitation. K est une constante de l'algorithme de fusion : la
#: déplacer sans re-mesurer déplace tous les classements à la fois.
RRF_K = 60


def _rrf_k() -> int:
    """Constante RRF. K gouverne l'amortissement : plus il est PETIT, plus l'écart
    entre les premiers rangs pèse (fusion « piquée ») ; plus il est grand, plus les
    rangs s'égalisent. Pour l'explorer, passer par un banc de mesure, pas par
    l'environnement d'un déploiement.
    """
    return RRF_K


def _safe_weight(w: Any) -> float:
    """Poids de fusion validé : fini, strictement positif. Repli sûr sur 1.0.

    Un NaN, un ±inf ou un poids ≤ 0 casserait le classement (NaN empoisonne tout tri) :
    on retombe alors sur le comportement neutre plutôt que de propager l'anomalie.
    """
    try:
        f = float(w)
    except (TypeError, ValueError):
        return 1.0
    if f != f or f in (float("inf"), float("-inf")) or f <= 0.0:
        return 1.0
    return f


def _rrf(rank: int | None, weight: float = 1.0) -> float:
    """Contribution d'une modalité : `poids / (K + rang)`. Rang absent -> aucune.

    Le poids par défaut 1.0 reproduit exactement la contribution historique.
    """
    return 0.0 if rank is None else weight / (_rrf_k() + rank)


@dataclass(frozen=True)
class FusionLeg:
    """Une liste de candidats CLASSÉE, prête à voter dans la fusion.

    ``modality`` dit comment lire le score brut (``text_score`` ou ``vector_score``) ;
    ``weight`` pondère le vote de rang. ``origin`` est purement descriptif — la fusion
    ne s'en sert pas pour classer : aucun quota, aucun privilège de provenance.
    """

    modality: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    weight: float = 1.0
    origin: str = ""


def merge_rrf(*, legs: list[FusionLeg], top_k: int) -> list[dict[str, Any]]:
    """Fusion de rangs réciproques sur un nombre QUELCONQUE de listes classées.

    Pourquoi cette généralisation. Le RRF est la seule opération du pipeline qui
    compare des listes dont les scores ne sont **pas** commensurables : il n'additionne
    pas des scores, il additionne des ``poids / (K + rang)``. C'est ce qui le rend
    valide entre un ``ts_rank`` (0,01–0,1) et un cosinus (0,4–0,9) — et, exactement pour
    la même raison, entre le leg lexical privé et le leg lexical d'un corpus métier.

    Le défaut que cela corrige. Les candidats corpus étaient injectés APRÈS
    ``merge_hybrid`` et n'obtenaient donc jamais de ``fusion_score``. La
    diversification, qui classe sur ``(fusion_score, score)``, lisait ``0.0`` pour
    eux : **tout** candidat corpus passait derrière **tout** candidat privé, quelle que
    soit sa pertinence. Mesuré : un chunk corpus à ``ts_rank`` deux fois supérieur au
    meilleur chunk privé n'était jamais sélectionné.

    Ce que cette fonction ne fait PAS : réserver des places, pondérer par provenance,
    ou forcer une citation. Un candidat corpus moins pertinent reste éliminé — il l'est
    désormais sur son rang, pas sur son origine.

    Contrat de sortie IDENTIQUE à ``merge_hybrid`` : ``score``, ``text_score``,
    ``vector_score``, ``text_rank``, ``vector_rank``, ``fusion_score``.
    """
    merged: dict[str, dict[str, Any]] = {}

    for leg in legs or []:
        w = _safe_weight(leg.weight)
        modality = leg.modality
        score_key = "text_score" if modality == MODALITY_TEXT else "vector_score"
        rank_key = "text_rank" if modality == MODALITY_TEXT else "vector_rank"

        for rank, ch in enumerate(leg.candidates or [], start=1):
            if not isinstance(ch, dict):
                continue
            cid = ch.get("chunk_id")
            if not cid:
                continue
            entry = merged.get(cid)
            if entry is None:
                # Le PREMIER leg où le candidat apparaît fournit le dict de base :
                # `merge_hybrid` avait la même règle (la liste lexicale d'abord).
                entry = {
                    **ch,
                    "text_score": 0.0,
                    "vector_score": 0.0,
                    "text_rank": None,
                    "vector_rank": None,
                    "fusion_score": 0.0,
                }
                merged[cid] = entry

            raw = float(ch.get("score") or 0.0)
            if raw > float(entry.get(score_key) or 0.0):
                entry[score_key] = raw
            previous_rank = entry.get(rank_key)
            if previous_rank is None or rank < previous_rank:
                entry[rank_key] = rank
            # Chaque leg vote une fois : la somme EST la définition du RRF.
            entry["fusion_score"] = float(entry.get("fusion_score") or 0.0) + (w / (_rrf_k() + rank))

    for ch in merged.values():
        ch["score"] = max(float(ch.get("text_score") or 0.0), float(ch.get("vector_score") or 0.0))

    ranked = sort_candidates(
        list(merged.values()),
        relevance_key=lambda x: (
            float(x.get("fusion_score") or 0.0),
            float(x.get("vector_score") or 0.0),
        ),
    )
    return ranked[: int(top_k)]


def merge_hybrid(
    *,
    text_chunks: list[dict[str, Any]],
    vector_chunks: list[dict[str, Any]],
    top_k: int,
    lexical_weight: float = 1.0,
    vector_weight: float = 1.0,
) -> list[dict[str, Any]]:
    """Fusion hybride déterministe, ordonnée par **rang** et non par score brut.

    🔴 Le bug que ceci corrige. L'ancien classement prenait
    ``score = max(text_score, vector_score)``. Or les deux échelles sont
    **incomparables** : ``ts_rank`` (FTS) sort typiquement 0,01–0,1, quand la
    similarité cosinus sort 0,4–0,9. Le ``max`` faisait donc gagner le vecteur
    **systématiquement** : le leg lexical était mort dès que le vecteur renvoyait
    quelque chose. Ce n'était pas une fusion — c'était une recherche vectorielle avec
    une étape inutile.

    Mesuré sur le cas qui a révélé le bug (un classeur financier dans un espace de
    revues de presse) : le FTS trouvait le classeur à ``ts_rank = 0,0585`` ; le vecteur
    ramenait des revues de presse hors sujet à ``cosinus = 0,7538``. Le ``max`` éjectait
    le classeur du top-k, et l'utilisateur recevait « Je ne sais pas » sur un document
    parfaitement indexé.

    Le dépôt avait DÉJÀ identifié cette incompatibilité d'échelles — ``hygiene.py``
    applique des seuils séparés par modalité (``RAG_MIN_TEXT_RANK`` vs
    ``RAG_MIN_SCORE``). Le **filtrage** était corrigé ; le **classement**, non.

    On classe donc par **fusion de rangs réciproques** (RRF) : chaque modalité vote avec
    son rang, jamais avec son score. C'est sans dimension, donc insensible aux échelles.
    Un chunk présent dans les DEUX listes cumule les deux votes — c'est là que la
    fusion prend enfin son sens.

    ``score`` reste ``max(text, vector)`` : il est exposé par l'API, lu par le rerank et
    par les seuils. On change l'ORDRE, pas le contrat.

    Cas particulier à DEUX legs de ``merge_rrf`` : mêmes rangs, mêmes poids, même
    tri, même troncature. La pondération lexicale conditionnelle (P1.5) reste
    appliquée ICI, avant la troncature à ``top_k`` — un reweight postérieur ne
    rattraperait jamais un chunk déjà coupé.
    """
    return merge_rrf(
        legs=[
            FusionLeg(modality=MODALITY_TEXT, candidates=text_chunks or [], weight=lexical_weight),
            FusionLeg(modality=MODALITY_VECTOR, candidates=vector_chunks or [], weight=vector_weight),
        ],
        top_k=top_k,
    )


def merge_candidates(*, a: list[dict[str, Any]] | None, b: list[dict[str, Any]] | None, top_k: int) -> list[dict[str, Any]]:
    """
    Merge two candidate lists deterministically by chunk_id, keeping best score and preserving
    any extra fields when available.
    """
    merged: dict[str, dict[str, Any]] = {}

    for ch in (a or []):
        cid = ch.get("chunk_id")
        if not cid:
            continue
        merged[cid] = ch

    for ch in (b or []):
        cid = ch.get("chunk_id")
        if not cid:
            continue
        if cid not in merged:
            merged[cid] = ch
        else:
            s_old = float(merged[cid].get("score") or 0.0)
            s_new = float(ch.get("score") or 0.0)
            if s_new > s_old:
                merged[cid] = {**merged[cid], **ch, "score": s_new}
            else:
                merged[cid] = {**ch, **merged[cid], "score": s_old}

    # `score` seul n'ordonne pas totalement : les candidats FTS sont massivement ex æquo
    # (ts_rank est très discret). Comme cette fonction TRONQUE (`[:top_k]`) et qu'elle est
    # appelée en cascade dans les boucles multi-passes de l'orchestrateur, un ordre
    # arbitraire ici se traduit par une SÉLECTION différente de candidats.
    out = sort_candidates(
        list(merged.values()),
        relevance_key=lambda x: float(x.get("score") or 0.0),
    )
    return out[: int(top_k)]
