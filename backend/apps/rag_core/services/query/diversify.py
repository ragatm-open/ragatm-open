from __future__ import annotations

from typing import Any, Callable

from apps.rag_core.domain.ranking import sort_candidates

"""
Diversification du pool de candidats — étape de SÉLECTION, jamais de score.

Le problème, mesuré sur un classeur financier. Après le découpage bidimensionnel des
feuilles larges, le bon chunk (« Summary Pro Forma / Premoney Capitalization ») est passé
du rang lexical 52 au rang 11 : il existe enfin, il est bien classé — mais le retrieval
tronquait à top_k=8 AVANT toute diversification. Un chunk au rang 11 ne pouvait donc
jamais entrer, quand bien même plusieurs chunks légitimes d'une MÊME feuille et d'un MÊME
groupe de colonnes occupaient les huit premières places.

On élargit donc le pool (candidate_k), on garde le classement RRF comme vérité, et on
sélectionne en DEUX passes :

1. on parcourt le pool dans l'ordre RRF, en s'autorisant au plus `max_per_group` chunks
   par groupe — ce qui empêche une feuille dense de monopoliser le top_k ;
2. si le top_k n'est pas rempli (pas assez de groupes distincts), on complète avec les
   meilleurs restants, plafond ignoré.

Le plafond est donc SOUPLE : il évite la monopolisation sans jamais interdire plusieurs
passages d'un même document quand une question l'exige. Le score RRF n'est jamais
modifié ; on ne fait que choisir un sous-ensemble.
"""


def _norm(v: Any) -> str:
    return str(v).strip().lower() if v not in (None, "") else ""


def _meta_of(chunk: dict[str, Any]) -> dict:
    src = chunk.get("source") if isinstance(chunk.get("source"), dict) else {}
    meta = src.get("meta") if isinstance(src.get("meta"), dict) else None
    if meta is None:
        meta = chunk.get("meta") if isinstance(chunk.get("meta"), dict) else {}
    return meta


def _document_id(chunk: dict[str, Any]) -> str:
    src = chunk.get("source") if isinstance(chunk.get("source"), dict) else {}
    return _norm(src.get("document_id"))


def build_diversity_group(chunk: dict[str, Any]) -> str:
    """Clé de groupe HIÉRARCHIQUE, avec repli générique.

        document_id + sheet_name + column_group      (chunk Excel structuré)
        document_id + sheet_name                      (Excel sans groupe de colonnes)
        document_id                                   (PDF, DOCX, CSV…)

    La **page** PDF n'est délibérément JAMAIS une clé : chaque page deviendrait un
    groupe distinct, et un PDF long raflerait toute la diversité au détriment des autres
    documents. Le repli générique reste donc le document entier.
    """
    doc = _document_id(chunk)
    meta = _meta_of(chunk)
    sheet = _norm(meta.get("sheet_name"))
    if not sheet:
        return doc or "∅"
    group = _norm(meta.get("column_group"))
    parts = [doc, sheet] + ([group] if group else [])
    return "\x1f".join(parts)


def rrf_sort_key(c: dict[str, Any]) -> tuple:
    """Clé de classement RRF **canonique** — la seule autorisée pour ordonner un pool.

    Exportée délibérément : tout consommateur qui doit prélever un sous-ensemble du pool
    (reranking, diagnostic, replay) DOIT trier avec cette clé au lieu de se fier à
    l'ordre courant de la liste. Cet ordre est celui de l'étape d'hygiène, qui trie par
    ``score`` brut — un ``chunks[:N]`` naïf prélève donc un AUTRE sous-ensemble.
    """
    return _rrf_key(c)


def _rrf_key(c: dict[str, Any]) -> tuple:
    """Clé de classement RRF, avec repli sur le score brut.

    ⚠️ Indispensable. Après `merge_hybrid`, l'étape d'hygiène RE-TRIE le pool par
    `score = max(text_score, vector_score)` — deux échelles incomparables (ts_rank ≈
    0,26 vs cosinus ≈ 0,75). Cela DÉTRUIT l'ordre RRF : les chunks vectoriels passeraient
    tous devant les chunks lexicaux, et la diversité, qui suppose un ordre RRF,
    sélectionnerait le mauvais sous-ensemble. On rétablit donc l'ordre RRF ici.

    Pour un pool mono-modalité (FTS seul, sans fusion), `fusion_score` est absent : le
    repli sur `score` conserve alors l'ordre lexical, qui EST le classement.

    Après reranking, `rerank_rank` porte l'ordre autoritaire — voir plus bas. Ni `score`
    ni `fusion_score` ne sont réécrits : ils continuent de dire ce que le retrieval
    pensait AVANT le reranking.
    """
    def _f(x: Any) -> float:
        try:
            return float(x or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # Un candidat passé par le reranker porte `rerank_rank` (1 = meilleur). Cet ordre
    # est AUTORITAIRE et prime sur la fusion : c'est la raison d'être du reranking.
    #
    # Le premier élément du tuple sépare les deux régimes, ce qui garantit que TOUT
    # candidat reranké précède les candidats non rerankés — la queue du pool, que le
    # reranker n'a pas vue. C'est exactement la propriété que l'ancien code obtenait
    # en écrasant `fusion_score` par un ordinal décroissant ; elle est désormais
    # obtenue sans mentir sur le sens d'un champ.
    rr = c.get("rerank_rank")
    if rr is not None:
        try:
            return (1.0, -float(rr), 0.0)
        except (TypeError, ValueError):
            pass
    return (0.0, _f(c.get("fusion_score")), _f(c.get("score")))


def diversify_ranked_results(
    results: list[dict[str, Any]],
    *,
    top_k: int,
    max_per_group: int,
    group_key: Callable[[dict[str, Any]], str] = build_diversity_group,
) -> list[dict[str, Any]]:
    """Sélectionne au plus `top_k` résultats en limitant chaque groupe. Deux passes.

    L'ordre RRF est rétabli en interne (cf. `_rrf_key`) : on ne se fie pas à l'ordre
    d'entrée, qu'une étape d'hygiène a pu re-trier par score brut. Le tri est **stable**,
    donc déterministe à rang égal. Aucun doublon. O(n log n) pour le tri, O(n) ensuite.
    """
    if top_k <= 0 or not results:
        return []
    if max_per_group <= 0:
        return list(results[:top_k])

    # Départage documentaire indispensable ICI : en pool mono-modalité (FTS seul,
    # RAG_HYBRID_ENABLED=0), `fusion_score` est absent et `_rrf_key` se réduit au
    # ts_rank — donc à des ex æquo massifs. La sélection ci-dessous applique le plafond
    # par groupe et tronque à `top_k` : sans ordre total, elle retiendrait un
    # sous-ensemble arbitraire. Cf. domain/ranking.py.
    results = sort_candidates(results, relevance_key=_rrf_key)

    selected: list[dict[str, Any]] = []
    picked: set[str] = set()
    counts: dict[str, int] = {}

    # Passe 1 — plafond STRICT par groupe.
    for r in results:
        cid = str(r.get("chunk_id") or "")
        if not cid or cid in picked:
            continue
        g = group_key(r)
        if counts.get(g, 0) >= max_per_group:
            continue
        selected.append(r)
        picked.add(cid)
        counts[g] = counts.get(g, 0) + 1
        if len(selected) >= top_k:
            return selected

    # Passe 2 — plafond IGNORÉ : on complète avec les meilleurs restants.
    for r in results:
        cid = str(r.get("chunk_id") or "")
        if not cid or cid in picked:
            continue
        selected.append(r)
        picked.add(cid)
        if len(selected) >= top_k:
            break

    return selected


def diversify_by_document(chunks: list[dict[str, Any]], *, top_k: int, min_unique_docs: int) -> list[dict[str, Any]]:
    """Force une couverture multi-doc (mode inventaire). Contrat historique préservé.

    Réécrit sur la primitive générique : une seule implémentation de la diversité, pour
    que l'inventaire et le retrieval normal ne divergent pas. La sémantique est
    inchangée — plafond de 1 chunk par DOCUMENT jusqu'à couvrir `min_unique_docs`
    documents distincts, puis complétion par le classement d'origine.
    """
    if not chunks or top_k <= 0:
        return []

    target = min(int(min_unique_docs), int(top_k)) if min_unique_docs else int(top_k)
    diversified = diversify_ranked_results(
        chunks, top_k=target, max_per_group=1, group_key=_document_id,
    )
    if len(diversified) >= top_k:
        return diversified[:top_k]

    # Complète jusqu'à top_k avec les meilleurs restants (ordre d'origine).
    picked = {str(c.get("chunk_id") or "") for c in diversified}
    out = list(diversified)
    for c in chunks:
        if len(out) >= top_k:
            break
        cid = str(c.get("chunk_id") or "")
        if not cid or cid in picked:
            continue
        out.append(c)
        picked.add(cid)
    return out
