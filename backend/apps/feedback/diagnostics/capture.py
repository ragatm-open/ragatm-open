"""Construction de la trace de candidats — module PUR (aucune I/O, aucun Django ORM).

Ce module transforme les listes de candidats VIVANTES de ``query_rag`` en une
structure normalisée, bornée et NON SENSIBLE.

Ce qui entre : les mêmes dictionnaires de chunks que le pipeline manipule.
Ce qui sort : des identifiants, des rangs, des scores et une raison d'exclusion.

Ce qui ne sort JAMAIS — et c'est vérifié par un test dédié :
texte de chunk, nom de fichier, chemin, question, réponse, prompt, commentaire
utilisateur, secret. Le tampon est écrit pour 100 % des réponses ; il doit donc
être aussi inoffensif qu'un compteur.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import reasons as R

#: Version du schéma de trace de candidats. À incrémenter à tout changement
#: INCOMPATIBLE (champ retiré, sémantique modifiée).
CANDIDATE_TRACE_SCHEMA_VERSION = 1

#: Étape du pipeline à laquelle le pool est observé. Écrit DANS la trace, et pas
#: seulement dans la documentation : c'est cette valeur qui empêche de lire
#: « absent de la trace » comme « jamais candidat ».
#:
#: Ce que la trace permet de dire, et ce qu'elle ne permet PAS de dire :
#:
#:   chunk PRÉSENT  -> son rang, son score et l'étape qui l'a écarté sont connus ;
#:   chunk ABSENT   -> il a disparu AVANT ou PENDANT le rappel initial. On ne
#:                     peut pas encore distinguer : absence FTS, absence
#:                     vectorielle, seuil interne du CandidateRetriever, filtre
#:                     de périmètre, ou défaut d'indexation.
OBSERVATION_BOUNDARY = "post_fusion"

#: Formulation IMPOSÉE pour un candidat absent. Le rapport d'incident doit dire
#: ceci, et jamais « jamais candidat » : la seconde formulation est une
#: conclusion que la trace ne soutient pas.
ABSENT_CANDIDATE_WORDING = (
    "absent du pool post-fusion (écarté avant ou pendant le rappel initial ; "
    "l'étape exacte n'est pas observable à ce point de capture)"
)


def _chunk_id(chunk: dict[str, Any]) -> str:
    return str(chunk.get("chunk_id") or "")


def _document_id(chunk: dict[str, Any]) -> str:
    source = chunk.get("source")
    if isinstance(source, dict) and source.get("document_id"):
        return str(source["document_id"])
    return str(chunk.get("document_id") or "")


def _num(value: Any) -> float | None:
    """Nombre fini, ou ``None``. Un NaN sérialisé en JSON casserait la relecture."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, 6)


def _leg(chunk: dict[str, Any]) -> str:
    """Origine du candidat, déduite des rangs déjà posés par la fusion."""
    explicit = chunk.get("retrieval_leg")
    if explicit:
        return str(explicit)
    has_text = chunk.get("text_rank") is not None
    has_vector = chunk.get("vector_rank") is not None
    if has_text and has_vector:
        return "hybrid"
    if has_vector:
        return "vector"
    if has_text:
        return "fts"
    return "unknown"


def _ids(chunks: Iterable[dict[str, Any]] | None) -> set[str]:
    return {_chunk_id(c) for c in (chunks or []) if _chunk_id(c)}


def build_candidate_trace(
    *,
    pool: list[dict[str, Any]] | None,
    selected_pre_rerank: list[dict[str, Any]] | None,
    selected_post_rerank: list[dict[str, Any]] | None,
    selected_final: list[dict[str, Any]] | None,
    top_k: int,
    rerank_applied: bool,
    diversity_applied: bool,
    structural_penalised_ids: set[str] | None = None,
    max_candidates: int = 20,
) -> dict[str, Any]:
    """Trace normalisée du pool de candidats, bornée à ``max_candidates``.

    La raison d'exclusion est déduite par COMPARAISON entre les états successifs
    de la sélection, jamais devinée :

    - présent dans le contexte final                      -> ``selected`` ;
    - retenu par le ranking, absent du final              -> ``prompt_budget`` ;
    - présent avant rerank, absent après                  -> ``rerank_cut`` ;
    - dans le ``top_k`` du pool mais jamais sélectionné,
      alors que la diversification a joué                 -> ``diversity_max_per_group`` ;
    - au-delà du ``top_k``                                -> ``not_selected_top_k`` ;
    - tout le reste                                       -> ``unknown``.
    """
    pool = list(pool or [])
    final_ids = _ids(selected_final)
    post_rerank_ids = _ids(selected_post_rerank)
    pre_rerank_ids = _ids(selected_pre_rerank)
    penalised = set(structural_penalised_ids or ())

    candidates: list[dict[str, Any]] = []
    for index, chunk in enumerate(pool[: max(0, int(max_candidates))]):
        cid = _chunk_id(chunk)
        if not cid:
            continue

        if cid in final_ids:
            reason = R.SELECTED
            selected = True
        else:
            selected = False
            if cid in post_rerank_ids:
                reason = R.PROMPT_BUDGET
            elif rerank_applied and cid in pre_rerank_ids:
                reason = R.RERANK_CUT
            elif index < int(top_k):
                # Le candidat était dans le `top_k` du pool classé et n'a pas été
                # retenu : seule la diversification peut l'expliquer ici.
                reason = R.DIVERSITY_MAX_PER_GROUP if diversity_applied else R.UNKNOWN
            elif index >= int(top_k):
                reason = R.NOT_SELECTED_TOP_K
            else:  # pragma: no cover - défensif
                reason = R.UNKNOWN

        entry = {
            "chunk_id": cid,
            "document_id": _document_id(chunk),
            "leg": _leg(chunk),
            "rank_fts": chunk.get("text_rank"),
            "rank_vector": chunk.get("vector_rank"),
            "rank_fused": index + 1,
            "score": _num(chunk.get("score")),
            "fusion_score": _num(chunk.get("fusion_score")),
            "rerank_score": _num(chunk.get("rerank_score")),
            "selected": selected,
            "excluded_by": None if selected else reason,
        }
        if cid in penalised:
            # Information ADDITIVE : la pénalité structurelle modifie le score de
            # classement sans écarter le candidat. Elle explique un déclassement,
            # elle n'est donc pas une raison d'exclusion.
            entry["structural_penalty_applied"] = True
        candidates.append(entry)

    return {
        "schema_version": CANDIDATE_TRACE_SCHEMA_VERSION,
        "pool_size": len(pool),
        "captured": len(candidates),
        "truncated": len(pool) > len(candidates),
        "candidates": candidates,
    }


def build_selection(
    *,
    selected_final: list[dict[str, Any]] | None,
    answerability_policy: str,
    thresholds: dict[str, Any] | None,
    budgets: dict[str, Any] | None,
) -> dict[str, Any]:
    """Décision de sélection : ce qui a été retenu, et sous quelles règles."""
    return {
        "selected_chunk_ids": [
            _chunk_id(c) for c in (selected_final or []) if _chunk_id(c)
        ],
        "answerability_policy": str(answerability_policy or ""),
        "thresholds": dict(thresholds or {}),
        "budgets": dict(budgets or {}),
    }


def build_trace_payload(
    *,
    log_id: str,
    created_at: str,
    embedding_model: str,
    candidate_trace: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    """Enveloppe finale écrite dans le tampon."""
    return {
        "schema_version": CANDIDATE_TRACE_SCHEMA_VERSION,
        "log_id": str(log_id),
        "created_at": str(created_at),
        "embedding_model": str(embedding_model or ""),
        "candidates": candidate_trace.get("candidates", []),
        "pool_size": candidate_trace.get("pool_size", 0),
        "truncated": bool(candidate_trace.get("truncated", False)),
        "selection": selection,
        # Frontière d'observation, transportée AVEC la donnée. Un consommateur
        # (admin, paquet d'incident, replay) qui lit cette trace sait ainsi ce
        # qu'une absence signifie — et ce qu'elle ne signifie pas.
        "observation_boundary": OBSERVATION_BOUNDARY,
        "absent_candidate_means": ABSENT_CANDIDATE_WORDING,
    }
