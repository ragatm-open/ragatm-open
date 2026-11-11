"""Éligibilité documentaire au retrieval — définition CANONIQUE (Lot 2 — P0-A).

INVARIANT DE SÉCURITÉ
--------------------
> Un chunk n'est utilisable — retrieval, reranking, citation, contexte LLM,
> réponse finale — que si son document est ``READY`` **et** si son RagSpace n'est
> pas supprimé.

POURQUOI UN MODULE DÉDIÉ
------------------------
Le rapport P0-A a montré que la règle était **réécrite ou omise requête par
requête** : cinq régimes de filtrage coexistaient, et le chemin le plus exposé —
le chat — était le **moins** filtré de tous. Les deux requêtes du retrieval
conversationnel (FTS et vectoriel), exécutées à chaque question, ne portaient
aucune clause de statut. Mesuré en base : 410 chunks rattachés à un document
``FAILED`` (dont un au rang #1 du leg vectoriel) et 25 847 chunks dans des espaces
soft-deleted.

La cause n'était pas une clause oubliée mais l'**absence de source unique**. Ce
module est cette source. Toute requête qui remonte des chunks DOIT en dériver.

APPLIQUER LE FILTRE EN SQL, AVANT ``LIMIT``
-------------------------------------------
Filtrer en Python après récupération fausserait le top-k : les candidats
inéligibles consommeraient des places dans la fenêtre, et une question dont les
meilleurs chunks sont inéligibles renverrait moins de résultats qu'elle ne le
devrait — voire aucun. Les fragments ci-dessous sont donc conçus pour être
injectés dans le ``WHERE``, pas appliqués après coup.

STRATÉGIE DE RÉINDEXATION — **Option A** (choisie et assumée)
-------------------------------------------------------------
Pendant une réindexation, le document passe ``INGESTING`` : ses anciens chunks
restent en base mais deviennent **inéligibles**. Il redevient interrogeable au
passage ``READY``, après remplacement atomique.

Conséquence acceptée : une indisponibilité temporaire du document pendant la
réindexation. Contrepartie obtenue : l'invariant est **trivialement vrai** — un
document que le système déclare non exploitable ne peut pas être servi, quel que
soit l'état de ses chunks. L'Option B (versions d'index publiée / en préparation)
serait sémantiquement plus fine mais introduit un état supplémentaire à maintenir
cohérent dans chaque requête — c'est-à-dire exactement la classe de défaut que ce
module supprime.

Corollaire : le reaper ``recover_stale_reindex_items`` peut continuer à marquer
``FAILED`` sans supprimer les chunks. Ce n'est plus une violation puisque
``FAILED`` n'est pas ``READY`` : le statut et l'éligibilité ne peuvent plus
diverger.

Voir ``docs/03-backend/eligibilite-retrieval.md``.
"""
from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

#: Le SEUL statut documentaire éligible au retrieval.
#: Volontairement un singleton et non une liste : élargir cet ensemble doit être
#: un acte délibéré, visible en revue, accompagné de tests.
ELIGIBLE_DOCUMENT_STATUS: str = "READY"


# --------------------------------------------------------------------------- #
# Fragments SQL (requêtes brutes)
# --------------------------------------------------------------------------- #
#
# Convention d'alias, respectée par tous les appelants :
#   d = table document (rag_document ou domain_corpus_document)
#   s = table rag_space (jointure ajoutée par SQL_RAG_SPACE_JOIN)
#
# Les fragments se terminent par un saut de ligne pour s'insérer proprement dans
# un WHERE multi-lignes.

#: Jointure nécessaire pour tester la suppression logique de l'espace.
SQL_RAG_SPACE_JOIN: str = "    JOIN rag_space s ON s.id = d.rag_space_id\n"

#: Clause de statut documentaire. `d` doit être l'alias du document.
SQL_DOCUMENT_STATUS_CLAUSE: str = (
    f"      AND d.status = '{ELIGIBLE_DOCUMENT_STATUS}'"
    "        -- 🔒 éligibilité : document exploitable\n"
)

#: Clause d'espace non supprimé. `s` doit être l'alias de rag_space.
SQL_RAG_SPACE_ALIVE_CLAUSE: str = (
    "      AND s.deleted_at IS NULL"
    "                 -- 🔒 éligibilité : espace non supprimé\n"
)

#: Les deux clauses réunies — le cas courant pour une requête sur `rag_chunk`.
SQL_ELIGIBILITY_CLAUSES: str = SQL_DOCUMENT_STATUS_CLAUSE + SQL_RAG_SPACE_ALIVE_CLAUSE


# --------------------------------------------------------------------------- #
# Prédicats ORM
# --------------------------------------------------------------------------- #


def eligible_documents_queryset(queryset: Any = None) -> Any:
    """QuerySet des documents éligibles au retrieval.

    ``queryset=None`` part de ``Document.objects``. Sinon, restreint le queryset
    fourni — ce qui permet de composer avec un filtre de périmètre existant sans
    dupliquer la règle d'éligibilité.
    """
    from apps.rag_core.models import Document

    qs = Document.objects.all() if queryset is None else queryset
    return qs.filter(
        status=ELIGIBLE_DOCUMENT_STATUS,
        rag_space__deleted_at__isnull=True,
    )


def eligible_chunks_queryset(queryset: Any = None) -> Any:
    """QuerySet des chunks éligibles (via leur document et son espace)."""
    from apps.rag_core.models import Chunk

    qs = Chunk.objects.all() if queryset is None else queryset
    return qs.filter(
        document__status=ELIGIBLE_DOCUMENT_STATUS,
        document__rag_space__deleted_at__isnull=True,
    )


def is_document_eligible(document: Any) -> bool:
    """True si ``document`` peut alimenter une réponse.

    Utilisé pour les revérifications ponctuelles (citations, moteur tabulaire),
    là où l'on tient déjà l'objet et où une requête serait superflue.
    """
    if document is None:
        return False
    if str(getattr(document, "status", "")) != ELIGIBLE_DOCUMENT_STATUS:
        return False
    space = getattr(document, "rag_space", None)
    if space is None:
        return False
    return getattr(space, "deleted_at", None) is None
