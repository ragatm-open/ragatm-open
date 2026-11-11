"""Battement de cœur de Celery Beat — écriture et lecture (Lot 4-G).

POURQUOI UN HEARTBEAT PLUTÔT QU'UN `pgrep`
------------------------------------------
Un processus Beat **vivant** peut ne plus rien ordonnancer : boucle bloquée sur
une connexion broker, horloge figée, scheduler en erreur silencieuse. Vérifier la
présence du processus répondrait « tout va bien » dans chacun de ces cas.

Le heartbeat prouve autre chose, et c'est la seule chose qui compte : **Beat a
effectivement déclenché une tâche, et un worker l'a exécutée**. Il traverse donc
toute la chaîne — scheduler → broker → queue → worker → base — et son absence
signale une rupture n'importe où sur ce trajet.

Le heartbeat est stocké dans Redis (déjà présent comme broker), avec un TTL
généreux : la fraîcheur est jugée sur l'horodatage écrit, pas sur l'existence de
la clé. Redis indisponible ⇒ heartbeat inconnu ⇒ santé « inconnue », jamais
« bonne » par défaut.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Clé Redis du dernier battement. Stable : la renommer masquerait l'historique.
HEARTBEAT_KEY = "ragatm:celery-beat:heartbeat"

#: TTL de la clé — largement supérieur au seuil de fraîcheur, pour qu'un
#: heartbeat périmé reste LISIBLE (et donc diagnosticable) au lieu de disparaître.
_HEARTBEAT_TTL_SECONDS = 86400

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"


def _redis_client():
    url = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
    if not url.startswith(("redis://", "rediss://", "unix://")):
        return None
    try:
        import redis

        return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except Exception:  # noqa: BLE001
        return None


def write_heartbeat() -> dict[str, Any]:
    """Écrit un battement daté. Ne lève jamais — un heartbeat n'est pas critique.

    Ne contient QUE des métadonnées d'exploitation : ni périmètre client, ni
    contenu, ni secret.
    """
    payload = {
        "timestamp": timezone.now().isoformat(),
        "instance_id": f"{socket.gethostname()}:{os.getpid()}",
        "environment": str(getattr(settings, "DJANGO_ENV", "") or os.getenv("DJANGO_ENV", "")),
        "version": str(getattr(settings, "RAGATM_VERSION", "") or ""),
    }
    client = _redis_client()
    if client is None:
        logger.warning("beat.heartbeat store_unavailable")
        return payload
    try:
        client.set(HEARTBEAT_KEY, json.dumps(payload), ex=_HEARTBEAT_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("beat.heartbeat write_failed")
    return payload


def read_heartbeat() -> dict[str, Any]:
    """Lit le dernier battement et évalue sa fraîcheur.

    Retourne ``{status, age_seconds, timestamp, instance_id, environment,
    max_age_seconds}``. ``status`` vaut :

    * ``ok``      — battement plus récent que le seuil ;
    * ``stale``   — battement présent mais trop ancien (Beat arrêté ou bloqué) ;
    * ``missing`` — aucun battement (Beat n'a jamais tourné depuis le TTL) ;
    * ``unknown`` — magasin injoignable : on ne SAIT pas, on ne prétend pas.
    """
    max_age = int(getattr(settings, "CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS", 180))
    out: dict[str, Any] = {
        "status": STATUS_UNKNOWN,
        "age_seconds": None,
        "timestamp": None,
        "instance_id": None,
        "environment": None,
        "max_age_seconds": max_age,
    }

    client = _redis_client()
    if client is None:
        return out

    try:
        raw = client.get(HEARTBEAT_KEY)
    except Exception:  # noqa: BLE001
        return out

    if not raw:
        out["status"] = STATUS_MISSING
        return out

    try:
        data = json.loads(raw)
        ts = timezone.datetime.fromisoformat(data["timestamp"])
    except Exception:  # noqa: BLE001
        out["status"] = STATUS_MISSING
        return out

    age = (timezone.now() - ts).total_seconds()
    out.update(
        timestamp=data.get("timestamp"),
        instance_id=data.get("instance_id"),
        environment=data.get("environment"),
        age_seconds=round(age, 1),
        status=STATUS_OK if age <= max_age else STATUS_STALE,
    )
    return out
