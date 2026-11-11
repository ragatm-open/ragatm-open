"""Sérialiser — et REVALIDER — les mutations structurelles d'un RagSpace.

## Ce que PostgreSQL fait déjà, et qu'il ne faut pas réécrire

Une insertion de `Document`/`Chunk` porte une clé étrangère vers `rag_space`.
PostgreSQL prend alors un verrou ``FOR KEY SHARE`` sur la ligne parente, pour empêcher
qu'elle disparaisse sous la nouvelle ligne. Ce verrou **entre en conflit** avec
``FOR UPDATE``.

Autrement dit : ``SELECT rag_space FOR UPDATE`` bloque déjà toute insertion concurrente
dans cet espace. Mesuré, deux connexions réelles — l'insertion attend, puis aboutit une
fois le verrou relâché. La « dernière race » consignée au lot précédent (« un document
entièrement nouveau n'a aucune ligne à verrouiller ») était exacte mais hors sujet :
c'est la ligne PARENTE qui est verrouillée, pas la ligne à naître.

## Ce que PostgreSQL ne fait PAS, et qui manquait

Sérialiser n'est pas revalider.

Un écrivain qui a capturé l'identité de l'espace AVANT d'attendre le verrou l'écrit
telle quelle une fois le verrou obtenu — même si l'espace a changé entre-temps. Mesuré :
une ingestion préparée sous ``ollama/nomic-embed-text`` publie son document dans un
espace devenu ``ai_gateway/legacy-768``. Le corpus migré reste cohérent ; le document
fraîchement ingéré, lui, devient **invisible** — la garde P1-D compare son empreinte à
celle de l'espace. Aucune erreur, aucune trace.

C'est ce que cette primitive ferme : elle verrouille **et** compare.

## Ce qu'elle ne fait pas

Elle ne bloque **jamais** les lecteurs : le retrieval ne prend pas ce verrou et n'a
aucune raison de l'attendre. Elle n'est pas non plus destinée à couvrir un calcul long
— on la prend juste avant d'écrire, jamais autour d'un appel Gateway ou d'une
extraction.
"""
from __future__ import annotations

import logging

from django.db import transaction

from apps.rag_core.models import RagSpace
from apps.rag_core.services import ops_counters

logger = logging.getLogger("rag.ragspace_mutation")

#: Compteur à cardinalité bornée : la RAISON du conflit, jamais l'identifiant d'espace.
COUNTER_CONFLICT = "rag_ragspace_mutation_conflict_total"

CONFLICT_IDENTITY_CHANGED = "identity_changed"


class RagSpaceIdentityChanged(RuntimeError):
    """L'identité de l'espace a changé pendant l'attente du verrou.

    Ce n'est pas une panne : c'est la détection exacte du cas que la primitive existe
    pour empêcher. L'appelant doit abandonner son résultat préparé et recommencer sous
    la nouvelle identité — jamais publier l'ancien.
    """

    def __init__(self, *, attendu: tuple[str, str], observe: tuple[str, str]) -> None:
        super().__init__(
            f"identité du RagSpace changée pendant l'attente du verrou : "
            f"préparée pour {attendu[0]}/{attendu[1]}, observée {observe[0]}/{observe[1]} — "
            "le résultat préparé n'est pas publiable dans cet espace"
        )
        self.attendu = attendu
        self.observe = observe


def _identite(space) -> tuple[str, str]:
    return ((space.embedding_provider or "").strip().lower(),
            (space.embedding_model or "").strip())


def lock_ragspace_for_mutation(
    rag_space_id, *, expected_identity: tuple[str, str] | None = None,
) -> RagSpace:
    """Verrouille la ligne RagSpace et rend l'instance verrouillée.

    À appeler **juste avant** les écritures, jamais autour d'un calcul.

    ## Pourquoi une fonction et non un gestionnaire de contexte

    Un ``with`` suggérerait que le verrou est relâché en sortie de bloc. Il ne l'est
    pas : un verrou de ligne PostgreSQL tient jusqu'au **commit** de la transaction.
    Un gestionnaire de contexte mentirait sur la portée, et quelqu'un finirait par
    croire qu'il peut en sortir pour appeler la Gateway.

    ``expected_identity`` — le couple ``(provider, model)`` sous lequel l'appelant a
    préparé son résultat. Fourni, il est comparé à l'identité PERSISTÉE une fois le
    verrou obtenu ; toute divergence lève `RagSpaceIdentityChanged` et rien n'est
    écrit. L'omettre revient à ne demander que la sérialisation — légitime pour une
    opération qui ne dépend pas de l'identité, comme une suppression.

    Ordre de verrouillage canonique du domaine, à respecter partout pour éviter les
    interblocages : **RagSpace, puis documents par clé primaire croissante**.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "lock_ragspace_for_mutation doit être appelée DANS une transaction : un "
            "verrou de ligne est relâché au commit, et hors transaction il le serait "
            "aussitôt — le verrou serait purement décoratif."
        )

    space = RagSpace.objects.select_for_update().get(pk=rag_space_id)

    if expected_identity is not None:
        attendu = ((expected_identity[0] or "").strip().lower(),
                   (expected_identity[1] or "").strip())
        observe = _identite(space)
        if observe != attendu:
            ops_counters.incr(COUNTER_CONFLICT)
            logger.warning(
                "ragspace_mutation.identity_changed attendu=%s/%s observé=%s/%s",
                attendu[0], attendu[1], observe[0], observe[1],
                extra={"reason": CONFLICT_IDENTITY_CHANGED},
            )
            raise RagSpaceIdentityChanged(attendu=attendu, observe=observe)

    return space
