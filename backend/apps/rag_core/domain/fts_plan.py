# backend/apps/rag_core/domain/fts_plan.py
from __future__ import annotations

"""Exécution d'un plan de requêtes FTS — stratégie UNIQUE, partagée par tous les legs.

Le problème que ce module résout
--------------------------------
`build_tsquery_plan` produit une liste de requêtes, de la plus **sélective** (entité
capitalisée, identifiant) à la plus **large** (disjonction de mots-clés). Le leg privé
parcourait ce plan et s'arrêtait à la première requête qui ramenait quelque chose ; le
leg corpus métier, lui, n'exécutait que `plan[0]`.

Conséquence mesurée : « **W**hat is venture capital ? » produit
`plan = ['what:*', 'what:* | venture:* | capital:*']`. Le corpus n'exécutait que
`what:*`, qui ne matche aucun de ses chunks — donc **zéro candidat**, sans erreur ni
trace. Le leg privé, lui, retombait sur la requête large et répondait. Un corpus métier
associé, actif et correctement indexé était ainsi absent du retrieval pour toute
question dont le premier mot est capitalisé — c'est-à-dire pour une phrase normale.

La stratégie de repli ne pouvait pas rester une boucle recopiée dans chaque leg : c'est
exactement ce qui a permis à la divergence de s'installer. Elle vit donc ICI, et nulle
part ailleurs.

Politique (identique au comportement historique du leg privé)
-------------------------------------------------------------
1. exécuter les requêtes du plan **dans l'ordre** ;
2. passer à la suivante **uniquement** sur zéro candidat exploitable ;
3. s'arrêter dès qu'une requête produit des candidats ;
4. ne **jamais** concaténer plusieurs requêtes du plan — un repli qui fusionnerait
   « entité » et « mots-clés » diluerait une recherche qui fonctionnait.

Le nombre de tentatives est **borné** (`RAG_FTS_PLAN_MAX_ATTEMPTS`) : le plan peut
contenir jusqu'à quatre références à slash en plus des deux requêtes historiques, et
une requête SQL par élément doit rester une dépense prévisible.
"""

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from django.conf import settings

#: Borne dure par défaut. Le plan le plus long que `build_tsquery_plan` sait produire
#: compte 6 éléments (4 références à slash + entité + mots-clés) : ce défaut ne tronque
#: donc aucun plan réel — il empêche seulement une dérive future de coûter N requêtes.
DEFAULT_MAX_FTS_PLAN_ATTEMPTS = 6


def max_plan_attempts() -> int:
    """Nombre maximal de requêtes FTS exécutées pour une même question."""
    try:
        n = int(getattr(settings, "RAG_FTS_PLAN_MAX_ATTEMPTS", DEFAULT_MAX_FTS_PLAN_ATTEMPTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FTS_PLAN_ATTEMPTS
    return n if n > 0 else DEFAULT_MAX_FTS_PLAN_ATTEMPTS


@dataclass(frozen=True)
class FtsPlanExecution:
    """Télémétrie NON SENSIBLE d'une exécution de plan.

    Ne contient ni la question, ni les requêtes tsquery, ni le moindre extrait : une
    tsquery est dérivée des mots de l'utilisateur et n'a rien à faire dans un log.
    Seuls des compteurs et un index sont conservés.
    """

    plan_size: int
    attempt_count: int
    plan_index: int | None
    attempt_row_counts: tuple[int, ...]
    truncated: bool
    #: Mode de construction de requête : "v1" (plan historique) ou "v2" (normalisée).
    #: Compteurs de JETONS uniquement — jamais les jetons eux-mêmes.
    query_mode: str = "v1"
    raw_term_count: int = 0
    normalized_term_count: int = 0
    duration_ms: int = 0

    @property
    def matched(self) -> bool:
        return self.plan_index is not None

    @property
    def fallback_used(self) -> bool:
        """Vrai quand ce n'est PAS la requête la plus sélective qui a répondu."""
        return self.plan_index is not None and self.plan_index > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_size": int(self.plan_size),
            "attempt_count": int(self.attempt_count),
            "plan_index": self.plan_index,
            "attempt_row_counts": list(self.attempt_row_counts),
            "truncated": bool(self.truncated),
            "fallback_used": bool(self.fallback_used),
            "query_mode": str(self.query_mode),
            "raw_term_count": int(self.raw_term_count),
            "normalized_term_count": int(self.normalized_term_count),
            "result_count": int(self.attempt_row_counts[-1]) if self.attempt_row_counts else 0,
            "duration_ms": int(self.duration_ms),
        }


EMPTY_EXECUTION = FtsPlanExecution(
    plan_size=0, attempt_count=0, plan_index=None, attempt_row_counts=(), truncated=False
)


def dedupe_rows(rows: Sequence[dict] | None) -> list[dict]:
    """Déduplique par ``chunk_id`` en préservant l'ordre (donc le classement SQL).

    Invariant explicite plutôt qu'implicite : la politique actuelle ne retient qu'UNE
    requête du plan, et une requête SQL ne peut pas rendre deux fois la même ligne. Si
    la politique évoluait vers une combinaison de plans, l'absence de doublon resterait
    garantie ici — au même endroit — plutôt que redécouverte dans chaque leg.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cid = row.get("chunk_id")
        key = str(cid) if cid is not None else ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out


def execute_fts_plan(
    *,
    plan: Sequence[str] | None,
    runner: Callable[[str], list[dict]],
    max_attempts: int | None = None,
    query_mode: str = "v1",
    raw_term_count: int = 0,
    normalized_term_count: int = 0,
) -> tuple[list[dict], FtsPlanExecution]:
    """Exécute ``plan`` via ``runner`` et renvoie ``(lignes, télémétrie)``.

    ``runner`` reçoit UNE requête tsquery et renvoie ses lignes. Il porte tout le SQL :
    ce module ne connaît ni table, ni périmètre, ni filtre d'éligibilité — c'est ce qui
    lui permet de servir le leg privé comme le leg corpus sans les coupler.

    Un plan vide renvoie ``([], EMPTY_EXECUTION)`` : c'est un résultat CONTRÔLÉ, pas une
    erreur. Une question sans terme exploitable ne doit jamais lever côté utilisateur.
    """
    queries = [q for q in (plan or []) if q]
    _meta = {"query_mode": query_mode, "raw_term_count": int(raw_term_count),
             "normalized_term_count": int(normalized_term_count)}
    if not queries:
        return [], replace(EMPTY_EXECUTION, **_meta)

    limit = int(max_attempts) if max_attempts is not None else max_plan_attempts()
    limit = max(1, limit)
    attempted = queries[:limit]
    truncated = len(queries) > len(attempted)

    counts: list[int] = []
    _started = time.perf_counter()
    for index, tsq in enumerate(attempted):
        rows = dedupe_rows(runner(tsq))
        counts.append(len(rows))
        if rows:
            return rows, FtsPlanExecution(
                plan_size=len(queries),
                attempt_count=len(counts),
                plan_index=index,
                attempt_row_counts=tuple(counts),
                truncated=truncated,
                duration_ms=int((time.perf_counter() - _started) * 1000),
                **_meta,
            )

    return [], FtsPlanExecution(
        plan_size=len(queries),
        attempt_count=len(counts),
        plan_index=None,
        attempt_row_counts=tuple(counts),
        truncated=truncated,
        duration_ms=int((time.perf_counter() - _started) * 1000),
        **_meta,
    )
