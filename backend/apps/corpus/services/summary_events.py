"""Événements structurés du résumé de corpus métier.

Pourquoi une allowlist et pas un simple ``logger.info(...)``
------------------------------------------------------------
La réponse HTTP de ce flux est volontairement opaque (``404 not_found`` pour cinq
causes distinctes). Le journal est donc le SEUL endroit où l'exploitant peut
distinguer « document inexistant » de « corpus non autorisé ». Il devient de ce
fait un point d'appel fréquent — et un candidat naturel à l'ajout d'un « juste le
début du texte, pour déboguer ».

L'allowlist rend cet ajout impossible sans modifier ce fichier : tout champ non
déclaré est **écarté**, pas tronqué. Un extrait de document, un prompt ou une clé
d'API passés par erreur n'atteignent jamais le log, et le test qui l'affirme n'a
qu'un seul endroit à surveiller.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Événements du cycle de vie d'un résumé métier. Noms STABLES : ils sont
#: consommés par l'exploitation et les tableaux de bord, pas seulement lus.
EVENT_REQUESTED = "domain_corpus_summary_requested"
EVENT_ACCESS_DENIED = "domain_corpus_summary_access_denied"
EVENT_NOT_FOUND = "domain_corpus_summary_not_found"
EVENT_STARTED = "domain_corpus_summary_started"
EVENT_COMPLETED = "domain_corpus_summary_completed"
EVENT_FAILED = "domain_corpus_summary_failed"
EVENT_CACHE_HIT = "domain_corpus_summary_cache_hit"

EVENTS = (
    EVENT_REQUESTED,
    EVENT_ACCESS_DENIED,
    EVENT_NOT_FOUND,
    EVENT_STARTED,
    EVENT_COMPLETED,
    EVENT_FAILED,
    EVENT_CACHE_HIT,
)

#: Seuls ces champs peuvent être journalisés. Aucun ne peut contenir de contenu
#: documentaire : ce sont des identifiants, des énumérations et des compteurs.
ALLOWED_FIELDS = frozenset(
    {
        "user_id",
        "workspace_id",
        "rag_space_id",
        "corpus_id",
        "document_id",
        "source_type",
        "scope",
        "page",
        "mode",
        "language",
        "request_id",
        "thread_id",
        "provider",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_usd",
        "duration_ms",
        "status",
        "error_code",
        # Cause INTERNE différenciée derrière un 404 volontairement générique.
        "reason",
        "chunk_count",
        "cached",
        "async",
        "summary_id",
    }
)


def _scalar(value: Any) -> Any:
    """Réduit une valeur à un scalaire journalisable, ou ``None``.

    Un UUID, un modèle ou une date deviennent une chaîne ; toute structure
    (dict/list) est ÉCARTÉE — c'est par là qu'un payload de source entrerait.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (dict, list, tuple, set)):
        return None
    return str(value)[:200]


def emit(event: str, **fields: Any) -> dict:
    """Journalise un événement. Ne lève JAMAIS : l'observabilité ne casse rien.

    Retourne la charge utile effectivement journalisée (utile aux tests, qui
    vérifient l'absence de contenu documentaire sans dépendre du format du log).
    """
    payload: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if key not in ALLOWED_FIELDS:
            continue
        coerced = _scalar(value)
        if coerced is None and value is not None:
            continue
        payload[key] = coerced
    try:
        logger.info("%s %s", event, payload)
    except Exception:  # pragma: no cover - un logger cassé ne doit rien annuler
        pass
    return payload
