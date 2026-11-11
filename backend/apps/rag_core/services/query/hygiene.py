# backend/apps/rag_core/services/query/hygiene.py
from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.rag_core.domain.ranking import sort_candidates


def _f(v) -> float:
    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def _vector_min() -> float:
    """Seuil pour les scores VECTORIELS (cosinus, ~0..1)."""
    return _f(getattr(settings, "RAG_MIN_SCORE", 0.0))


def _text_min() -> float:
    """Seuil pour les scores FTS (ts_rank, valeurs typiquement faibles ~0.0..0.1)."""
    return _f(getattr(settings, "RAG_MIN_TEXT_RANK", 0.0))


def _candidate_passes(ch: dict[str, Any], *, vector_min: float, text_min: float) -> bool:
    """
    Décide si un chunk candidat passe le filtre de score.

    IMPORTANT (correctif): les scores FTS (`ts_rank`) et vectoriels (cosinus)
    ne sont PAS sur la même échelle. On ne doit donc jamais filtrer un candidat
    FTS avec le seuil vectoriel `RAG_MIN_SCORE` (≈0.20) — un ts_rank pertinent
    vaut souvent 0.03..0.10 et serait jeté à tort avant la fusion/rerank.

    - Candidat POST-FUSION (porte `text_score` et `vector_score`) : on garde
      s'il passe le seuil d'AU MOINS une modalité (lexicale OU vectorielle).
    - Candidat MONO-MODALITÉ (pré-fusion, seulement `score`) : on applique le
      seuil passé par l'appelant (`vector_min`), qui doit correspondre à la
      modalité de la liste nettoyée (RAG_MIN_TEXT_RANK pour le FTS, RAG_MIN_SCORE
      pour le vectoriel).
    """
    has_modality = ("text_score" in ch) or ("vector_score" in ch)
    if has_modality:
        text_score = _f(ch.get("text_score"))
        vector_score = _f(ch.get("vector_score"))
        if text_score > 0.0 and text_score >= text_min:
            return True
        if vector_score > 0.0 and vector_score >= vector_min:
            return True
        return False

    # Liste mono-modalité : le seuil pertinent est fourni par l'appelant.
    return _f(ch.get("score")) >= vector_min


def dedupe_preserving_order(chunks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Nettoyage qui **ne classe pas** : invalides + doublons, ordre d'entrée PRÉSERVÉ.

    Destiné aux étapes POSTÉRIEURES à un classement qui fait autorité — aujourd'hui le
    reranking. `clean_candidates` y était inutilisable pour deux raisons, chacune
    suffisante :

    1. **il retrie par `score`**, donc il détruit le classement que le reranker vient de
       produire, juste avant la troncature `reranked[:rerank_take]`. Le reranker était
       exécuté, mais son résultat jeté ;
    2. **il refiltre au seuil vectoriel** : appelé sans `min_score`, il compare un
       `ts_rank` (~0,06) à `RAG_MIN_SCORE` (0,76) sur un candidat mono-modalité, et
       anéantit la liste entière. `reranked[:take]` devenait `[]`, et le repli
       `or selected` restaurait l'ordre retrieval **en silence**.

    Refiltrer ici serait de toute façon redondant : tout candidat qui atteint le rerank a
    déjà traversé `clean_candidates` avec le seuil de SA modalité (cf. orchestrator,
    legs lexical/vectoriel puis post-fusion).

    Contrat :
    - retire les non-dict et les candidats sans `chunk_id` ;
    - normalise `text` en `str` propre ;
    - déduplique par `chunk_id` en gardant la **première** occurrence — dans une liste
      rerankée, la première EST la meilleure selon le reranker ;
    - **ne trie pas, ne filtre pas par score**.

    Le filtrage et la déduplication sont **stables** : `[B, A, C]` avec `A` invalide
    donne `[B, C]`, jamais `[C, B]`.
    """
    if not chunks:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for ch in chunks:
        if not isinstance(ch, dict):
            continue

        cid = ch.get("chunk_id") or ch.get("id")
        if not cid:
            continue
        cid = str(cid)
        if cid in seen:
            continue
        seen.add(cid)

        raw_text = ch.get("text") or ""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text)
        ch["text"] = raw_text.strip()

        out.append(ch)

    return out


def clean_candidates(
    chunks: list[dict[str, Any]] | None,
    *,
    min_score: float | None = None,
    min_text_rank: float | None = None,
) -> list[dict[str, Any]]:
    """
    - Supprime les chunks sans chunk_id
    - Filtre par score, en respectant la MODALITÉ du candidat (cf.
      `_candidate_passes`) : un candidat FTS n'est jamais jeté au seuil vectoriel.
    - Force text à être une str propre
    - Déduplique par chunk_id (garde meilleur score)
    - Trie décroissant par score

    ⚠️ **Cette fonction CLASSE.** Ne jamais l'appeler après une étape dont l'ordre fait
    autorité (le reranking) : elle réordonnerait par le score de retrieval antérieur.
    Utiliser `dedupe_preserving_order` dans ce cas.

    Paramètres optionnels :
    - `min_score` : seuil appliqué aux candidats mono-modalité (défaut
      RAG_MIN_SCORE). Pour une liste FTS pré-fusion, passer RAG_MIN_TEXT_RANK.
    - `min_text_rank` : seuil lexical pour les candidats post-fusion (défaut
      RAG_MIN_TEXT_RANK).
    """
    if not chunks:
        return []

    vector_min = _vector_min() if min_score is None else _f(min_score)
    text_min = _text_min() if min_text_rank is None else _f(min_text_rank)

    by_id: dict[str, dict[str, Any]] = {}

    for ch in chunks:
        if not isinstance(ch, dict):
            continue

        cid = ch.get("chunk_id") or ch.get("id")
        if not cid:
            continue
        cid = str(cid)

        score = _f(ch.get("score"))

        # garde si le candidat passe le filtre adapté à sa modalité
        if not _candidate_passes(ch, vector_min=vector_min, text_min=text_min):
            continue

        raw_text = ch.get("text") or ""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text)
        ch["text"] = raw_text.strip()

        prev = by_id.get(cid)
        if prev is None:
            by_id[cid] = ch
            continue

        try:
            prev_score = float(prev.get("score") or 0.0)
        except Exception:
            prev_score = 0.0

        if score > prev_score:
            by_id[cid] = ch

    # Départage documentaire : `score` seul laisse les ex æquo dans l'ordre d'insertion
    # du dict, donc dans celui du SQL. Cf. domain/ranking.py.
    return sort_candidates(
        list(by_id.values()),
        relevance_key=lambda x: float(x.get("score") or 0.0),
    )
