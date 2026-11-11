# backend/apps/rag_core/domain/ranking.py
from __future__ import annotations

"""
Ordre TOTAL du classement de retrieval — source unique de vérité.

Le problème que ce module résout
--------------------------------
`ORDER BY text_rank DESC` n'est **pas** un ordre total : sur une requête mono-terme,
des dizaines de chunks partagent exactement la même valeur de `ts_rank` (mesuré : 30
chunks au texte identique → 1 seule valeur distincte). SQL ne définit alors AUCUN ordre
entre eux, et PostgreSQL est libre de les rendre dans l'ordre qui l'arrange : il dépend
du plan choisi (seq scan / bitmap scan), de l'ordre physique des lignes, du `VACUUM`, et
de la méthode de tri (`top-N heapsort`, qui n'est pas stable). Le `LIMIT k` tranche alors
au milieu du paquet d'ex æquo et retient un sous-ensemble **arbitraire**.

Conséquence mesurée, sur données équivalentes et question identique : après une
réindexation partielle (suppression de chunks + `VACUUM` + recréation), le top-5 change
**intégralement**. Ce n'est pas qu'un flake de tests — c'est la même question qui reçoit
des citations différentes en production, et un benchmark dont l'écart entre deux runs ne
mesure plus le moteur.

La règle
--------
1. **le score métier** — INCHANGÉ, c'est la pertinence et elle décide seule tant qu'elle
   discrimine ;
2. **`document_id`** puis **`chunk_index`** — la position DOCUMENTAIRE. Départage naturel
   (à pertinence égale, on lit un document dans l'ordre) et surtout **stable à la
   réindexation** : `Document.id` survit à une réingestion, et `chunk_index` est reproduit
   à l'identique par le chunker déterministe ;
3. **`chunk_id`** — garantie d'ordre **total**. `(document_id, chunk_index)` est unique en
   pratique mais aucune contrainte de base ne l'impose ; sans cette dernière clé, l'ordre
   ne serait total que par convention.

Pourquoi PAS `chunk_id` seul (le réflexe naturel) : `Chunk.id` est un `uuid4()` **régénéré
à chaque réindexation** (les chunks sont supprimés puis recréés). Un départage sur ce seul
identifiant serait total, mais l'ordre changerait à chaque réingestion d'un document
inchangé — on aurait remplacé un non-déterminisme par un autre, plus discret.

Pourquoi PAS `page_number` : nullable (`None` pour un classeur et une présentation) et
redondant — `chunk_index` est déjà globalement croissant dans un document, pages comprises.

Ce départage est FONCTIONNEL : il garantit la reproductibilité des citations en
production, pas seulement la stabilité de pytest. **Ne pas le retirer.**
"""

from typing import Any

# Fragment SQL à concaténer APRÈS le tri de pertinence, dans le MÊME `ORDER BY` que celui
# qui porte le `LIMIT` — sinon le moteur choisirait ses ex æquo avant le départage, et le
# tri secondaire n'ordonnerait qu'un sous-ensemble déjà arbitraire.
#
# `c` doit être l'alias de la table des chunks. Aucun paramètre : littéral sûr.
SQL_TIE_BREAK = "c.document_id ASC, c.chunk_index ASC, c.id ASC"


def _document_id_of(candidate: dict[str, Any]) -> str:
    src = candidate.get("source")
    if isinstance(src, dict):
        v = src.get("document_id")
        if v is not None:
            return str(v)
    v = candidate.get("document_id")
    return str(v) if v is not None else ""


def _chunk_index_of(candidate: dict[str, Any]) -> int:
    """Position du chunk dans son document.

    Lue au niveau RACINE du candidat (les requêtes de retrieval la sélectionnent
    explicitement). On ne la cherche PAS dans `meta` : c'est un JSON libre, dont la
    présence et le type ne sont garantis par rien.
    """
    v = candidate.get("chunk_index")
    if isinstance(v, bool) or not isinstance(v, int):
        # Absente ou mal typée : on ne devine pas. Le départage retombe sur `chunk_id`,
        # qui reste total. Une valeur par défaut haute évite d'inventer une position en
        # tête de document pour un candidat qui n'en a pas.
        return 1 << 31
    return v


def _chunk_id_of(candidate: dict[str, Any]) -> str:
    v = candidate.get("chunk_id")
    return str(v) if v is not None else ""


def tie_break_key(candidate: dict[str, Any]) -> tuple[str, int, str]:
    """Clé de départage déterministe, à ordre CROISSANT.

    À utiliser en dernière position d'un tri Python, après les clés de pertinence.
    Attention au sens : les tris de pertinence sont décroissants (`reverse=True`), ce
    départage est croissant. Les composer dans un même `sorted(reverse=True)` inverserait
    le départage — utiliser `sort_candidates` plutôt que de les mélanger à la main.
    """
    return (_document_id_of(candidate), _chunk_index_of(candidate), _chunk_id_of(candidate))


def sort_candidates(
    candidates: list[dict[str, Any]],
    *,
    relevance_key,
) -> list[dict[str, Any]]:
    """Trie par pertinence DÉCROISSANTE puis départage documentaire CROISSANT.

    `relevance_key` renvoie la clé de pertinence (float ou tuple de floats), triée en
    décroissant. Le départage, lui, est croissant — d'où les deux passes : un `sorted`
    unique avec `reverse=True` inverserait aussi le départage, et ferait remonter le
    dernier chunk d'un document avant le premier.

    Deux `sorted` successifs sont corrects parce que le tri de Python est **stable** :
    la seconde passe préserve l'ordre de la première à pertinence égale.
    """
    by_tie_break = sorted(candidates, key=tie_break_key)
    return sorted(by_tie_break, key=relevance_key, reverse=True)
