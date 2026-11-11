"""Socle des tâches périodiques : verrou distribué, métriques, bornes (Lot 4).

POURQUOI CE MODULE
------------------
Les tâches planifiées ne se comportent pas comme les tâches déclenchées par un
utilisateur : elles se **rejouent indéfiniment**, sans personne pour constater un
échec. Trois propriétés deviennent alors obligatoires, et aucune n'est offerte
par Celery :

1. **Exclusion mutuelle** — Beat peut redéclencher une tâche avant la fin de la
   précédente (exécution lente, worker relancé, deux Beat par accident). Deux
   purges concurrentes sur les mêmes lignes, c'est au mieux du travail inutile,
   au pire un interblocage.
2. **Bornes** — une purge non bornée qui rencontre un an d'arriéré tient une
   transaction ouverte pendant des minutes et bloque l'écriture.
3. **Observabilité** — une tâche périodique qui échoue en silence est
   indiscernable d'une tâche qui n'a jamais été planifiée. C'est exactement le
   défaut relevé par l'audit : `purge_expired_audit_logs` existait sans être
   ordonnancée, et rien ne le signalait.

Le verrou s'appuie sur Redis (`SET NX EX`), qui est déjà le broker Celery : pas
de dépendance nouvelle. **Il expire toujours** — un worker tué ne laisse jamais un
verrou immortel derrière lui.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from django.conf import settings

from apps.rag_core.tabular import observability as obs

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Noms de compteurs — STABLES (cf. convention tabular/observability.py)
# --------------------------------------------------------------------------- #
PERIODIC_RUNS = "ragatm_periodic_task_runs_total"
PERIODIC_FAILURES = "ragatm_periodic_task_failures_total"
PERIODIC_DURATION = "ragatm_periodic_task_duration_seconds"
PERIODIC_LOCK_SKIPS = "ragatm_periodic_task_lock_skips_total"

#: Préfixe des clés de verrou dans Redis. Stable : un renommage libérerait
#: silencieusement tous les verrous en cours au moment du déploiement.
LOCK_KEY_PREFIX = "ragatm:periodic-lock:"


class LockNotAcquired(RuntimeError):
    """Le verrou est déjà tenu ailleurs. Ce n'est PAS une erreur d'exploitation.

    Une tâche périodique qui saute un tour parce que la précédente tourne encore
    se comporte correctement. On la compte (``PERIODIC_LOCK_SKIPS``) pour repérer
    une tâche qui déborde systématiquement de sa fenêtre, mais on ne la compte
    jamais comme un échec.
    """


def _redis_client():
    """Client Redis dérivé du broker Celery. ``None`` si indisponible."""
    url = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
    if not url.startswith(("redis://", "rediss://", "unix://")):
        return None
    try:
        import redis  # dépendance déjà présente (broker Celery)

        return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except Exception:  # noqa: BLE001
        logger.warning("periodic.lock redis_client_unavailable")
        return None


def redis_client():
    """Client Redis des tâches périodiques (verrous, curseurs). ``None`` si indisponible.

    Exposé pour que les tâches qui ont besoin d'un état inter-exécutions (un
    curseur de déduplication, par exemple) réutilisent la MÊME dérivation depuis
    le broker Celery, au lieu d'en refaire une variante à côté.
    """
    return _redis_client()


def instance_id() -> str:
    """Identifiant du processus courant — pour tracer QUI tient le verrou."""
    return f"{socket.gethostname()}:{os.getpid()}"


@contextmanager
def distributed_lock(name: str, *, ttl_seconds: int | None = None) -> Iterator[str]:
    """Verrou exclusif nommé, à expiration garantie.

    Lève :class:`LockNotAcquired` si le verrou est déjà tenu — l'appelant décide
    quoi en faire (typiquement : sauter ce tour).

    **Sans Redis joignable, le verrou est accordé** plutôt que refusé. C'est un
    choix délibéré et le seul endroit de ce lot qui n'est pas fail-closed : bloquer
    toutes les tâches de maintenance parce que le broker est momentanément
    indisponible transformerait une panne d'infrastructure en arrêt du nettoyage.
    Le risque résiduel — deux exécutions concurrentes — est couvert par
    l'idempotence exigée de chaque tâche.
    """
    ttl = int(
        ttl_seconds
        if ttl_seconds is not None
        else getattr(settings, "PERIODIC_TASK_LOCK_TTL_SECONDS", 900)
    )
    key = f"{LOCK_KEY_PREFIX}{name}"
    token = uuid.uuid4().hex
    client = _redis_client()

    if client is None:
        logger.warning("periodic.lock unavailable name=%s -> exécution sans verrou", name)
        yield token
        return

    try:
        acquired = bool(client.set(key, token, nx=True, ex=ttl))
    except Exception:  # noqa: BLE001
        logger.warning("periodic.lock redis_error name=%s -> exécution sans verrou", name)
        yield token
        return

    if not acquired:
        obs.incr(PERIODIC_LOCK_SKIPS)
        logger.info("periodic.lock already_held name=%s", name)
        raise LockNotAcquired(name)

    try:
        yield token
    finally:
        # Libération CONDITIONNELLE : on ne supprime la clé que si elle porte
        # encore NOTRE jeton. Sinon, une tâche qui a dépassé le TTL supprimerait
        # le verrou d'une exécution suivante, légitime — et deux instances
        # tourneraient en parallèle en croyant l'inverse.
        try:
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end"
            )
            client.eval(script, 1, key, token)
        except Exception:  # noqa: BLE001
            logger.warning("periodic.lock release_failed name=%s (expirera seul)", name)


@contextmanager
def periodic_run(name: str, *, lock: bool = True, lock_ttl: int | None = None):
    """Enveloppe d'exécution d'une tâche périodique : verrou, mesure, journal.

    Usage ::

        with periodic_run("purge_expired_audit_logs") as run:
            run["deleted"] = purge(...)

    Le dictionnaire cédé sert à remonter des compteurs métier dans le journal
    final. Il ne doit contenir que des **nombres et des identifiants techniques**
    — jamais de contenu documentaire.
    """
    started = time.monotonic()
    payload: dict[str, Any] = {}
    obs.incr(PERIODIC_RUNS)

    try:
        if lock:
            with distributed_lock(name, ttl_seconds=lock_ttl):
                yield payload
        else:
            yield payload
    except LockNotAcquired:
        logger.info("periodic.skipped name=%s reason=lock_held", name)
        raise
    except Exception as exc:  # noqa: BLE001
        obs.incr(PERIODIC_FAILURES)
        duration = time.monotonic() - started
        obs.incr(PERIODIC_DURATION, duration)
        # Type d'exception seulement : un message d'erreur peut contenir des
        # données (paramètres SQL, réponse d'un tiers).
        logger.error(
            "periodic.failed name=%s error_type=%s duration_ms=%d",
            name, type(exc).__name__, int(duration * 1000),
        )
        raise
    else:
        duration = time.monotonic() - started
        obs.incr(PERIODIC_DURATION, duration)
        obs.incr(f"{PERIODIC_DURATION}_count")
        extras = " ".join(f"{k}={v}" for k, v in sorted(payload.items()))
        logger.info(
            "periodic.completed name=%s duration_ms=%d %s",
            name, int(duration * 1000), extras,
        )
