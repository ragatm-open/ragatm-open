"""Compteurs d'exploitation partagés entre processus.

## Pourquoi ce module existe

Le backend tourne sous **gunicorn avec plusieurs workers** (4 en production). Un compteur
tenu dans une variable de module est donc **local à un worker** : un exploitant qui lirait
celui d'un processus verrait un quart de la réalité, sans le savoir. Publier une telle
valeur comme métrique système serait pire que ne rien publier.

Ce module s'appuie sur le **cache Django**, déjà adossé à Redis et déjà partagé entre le
backend et tous les workers — c'est la primitive d'état inter-processus du projet, pas une
dépendance nouvelle.

## Le cas dégradé est déclaré, jamais masqué

Quand ``DJANGO_CACHE_URL`` est vide, Django retombe sur ``LocMemCache``, qui est de nouveau
**local au processus**. ``aggregation_mode()`` le signale, et l'exposition doit relayer
l'information : une métrique agrégée à tort est un mensonge silencieux.

## Sémantique

Compteurs **monotones** pour la durée de vie du magasin. Ils **ne survivent pas** à un
redémarrage de Redis — ce qui est acceptable pour une supervision fondée sur des taux et des
deltas, et doit être dit plutôt que sous-entendu.
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import caches

logger = logging.getLogger("rag.ops.counters")

#: Alias de cache dédié. MÊME service Redis que `default`, mais avec un budget d'E/S
#: propre : une contention de persistance côté serveur ne doit jamais s'ajouter à une
#: requête utilisateur. Repli sur `default` si l'alias n'est pas configuré (anciens
#: réglages, environnements de test minimaux).
CACHE_ALIAS = "ops"

#: Préfixe des clés. Les compteurs ne portent JAMAIS de dimension libre (question,
#: identifiant de fragment, utilisateur) : c'est ce qui fait exploser une série temporelle.
KEY_PREFIX = "ops:counter:"

#: Sans expiration : un compteur qui s'efface tout seul rendrait les taux faux.
TIMEOUT = None


def _cache():
    """Le cache des compteurs — dédié s'il existe, `default` sinon."""
    try:
        return caches[CACHE_ALIAS]
    except Exception:  # noqa: BLE001 - alias absent
        return caches["default"]


def _alias() -> str:
    return CACHE_ALIAS if CACHE_ALIAS in (settings.CACHES or {}) else "default"


def aggregation_mode() -> str:
    """``shared`` si le cache est partagé entre processus, ``process_local`` sinon."""
    try:
        backend = settings.CACHES[_alias()]["BACKEND"]
    except Exception:  # pragma: no cover - configuration exotique
        return "unknown"
    return "process_local" if "locmem" in backend.lower() else "shared"


def _pipeline_writer():
    """Rend `(client_redis, make_key)` si l'écriture groupée est possible, sinon `None`.

    Le regroupement importe pour une raison mesurée : le serveur Redis persiste en
    `appendfsync everysec`, et une écriture peut attendre la fin d'une `fsync` en cours
    (`aof_delayed_fsync`). Quatre écritures indépendantes offrent quatre occasions de
    heurter cette attente ; un pipeline n'en offre qu'une, dans une seule passe de la
    boucle d'événements.

    Accessoirement, `INCRBY` crée la clé absente : la vérification d'existence que
    `django_redis.cache.incr` impose (un `EXISTS` par incrément) disparaît.
    """
    c = _cache()
    client = getattr(c, "client", None)
    if client is None or not hasattr(client, "get_client") or not hasattr(client, "make_key"):
        return None
    try:
        return client.get_client(write=True), client.make_key
    except Exception:  # noqa: BLE001 - client indisponible
        return None


def incr_many(pairs: dict[str, int]) -> None:
    """Applique plusieurs compteurs en **un seul aller-retour** quand c'est possible.

    Ne lève jamais. En cas d'échec du pipeline, **aucun repli n'est tenté** : une
    exécution partielle côté serveur suivie d'un repli produirait un double comptage,
    ce qui est pire qu'un compteur manquant.
    """
    if not pairs:
        return
    try:
        writer = _pipeline_writer()
    except Exception as exc:  # noqa: BLE001 - la garantie « ne lève jamais » couvre TOUT
        logger.debug("ops_counter_writer_unavailable error=%s", type(exc).__name__)
        return
    if writer is not None:
        client, make_key = writer
        try:
            pipe = client.pipeline(transaction=False)
            for name, value in pairs.items():
                pipe.incrby(make_key(KEY_PREFIX + name), int(value))
            pipe.execute()
            return
        except Exception as exc:  # noqa: BLE001 - jamais bloquant
            logger.debug("ops_counter_pipeline_failed n=%d error=%s", len(pairs), type(exc).__name__)
            return
    for name, value in pairs.items():
        _incr_via_cache_api(name, value)


def _incr_via_cache_api(name: str, value: int) -> None:
    """Chemin générique — LocMem, ou client Redis indisponible."""
    c = _cache()
    key = KEY_PREFIX + name
    try:
        try:
            c.incr(key, int(value))
        except ValueError:
            # La clé n'existe pas encore. `add` ne remplace pas une valeur concurrente,
            # donc deux workers qui démarrent ensemble ne s'écrasent pas.
            c.add(key, 0, TIMEOUT)
            c.incr(key, int(value))
    except Exception as exc:  # noqa: BLE001 - jamais bloquant
        logger.debug("ops_counter_incr_failed name=%s error=%s", name, type(exc).__name__)


def incr(name: str, value: int = 1) -> None:
    """Incrémente UN compteur. Ne lève jamais : l'observabilité ne coûte pas une requête.

    Préférer `incr_many` quand plusieurs compteurs relèvent du même événement.
    """
    incr_many({name: value})


def get(name: str) -> int:
    try:
        return int(_cache().get(KEY_PREFIX + name) or 0)
    except Exception:  # noqa: BLE001
        return 0


def snapshot(names: list[str]) -> dict[str, int]:
    """Lit un ensemble de compteurs. Les absents valent 0, jamais ``None``."""
    return {n: get(n) for n in names}


def reset(names: list[str]) -> None:
    """Remise à zéro — réservée aux tests."""
    for n in names:
        try:
            _cache().delete(KEY_PREFIX + n)
        except Exception:  # noqa: BLE001
            pass


def describe() -> dict[str, Any]:
    """Métadonnées d'interprétation, exposées AVEC les valeurs.

    Un exploitant doit pouvoir dire, sans lire le code, si le chiffre qu'il regarde couvre
    l'ensemble des processus et ce qu'un redémarrage lui fait.
    """
    mode = aggregation_mode()
    return {
        "aggregation": mode,
        "aggregated_across_workers": mode == "shared",
        "durable_across_app_restart": mode == "shared",
        "durable_across_store_restart": False,
        "semantics": "monotonic counters; use rates/deltas, not absolute values",
    }


__all__ = ["CACHE_ALIAS", "KEY_PREFIX", "aggregation_mode", "describe", "get", "incr",
           "incr_many", "reset", "snapshot"]
