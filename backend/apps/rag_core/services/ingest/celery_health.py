"""Disponibilité d'un worker Celery (healthcheck léger, mis en cache).

Sert au frontend pour savoir si la réindexation durable dispose d'un worker
asynchrone (sinon mode eager : les gros jobs sont refusés). Ne lève jamais.
"""
from __future__ import annotations

import time

from django.conf import settings

_CACHE: dict = {"value": None, "at": 0.0}
_TTL_SECONDS = 15.0


def _is_eager() -> bool:
    return bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))


def _ping_worker(timeout: float = 1.5) -> bool:
    try:
        from apps.rag_core.tasks import run_reindex_job_task

        replies = run_reindex_job_task.app.control.ping(timeout=timeout)
        return bool(replies)
    except Exception:
        return False


def celery_worker_available(*, use_cache: bool = True) -> bool:
    """True si un worker Celery répond (ou mode eager). Résultat caché ~15 s."""
    if _is_eager():
        # Eager : le « worker » est inline. Async indisponible mais le contrat
        # 202 fonctionne pour les petits corpus.
        return False
    now = time.monotonic()
    if use_cache and _CACHE["value"] is not None and (now - _CACHE["at"]) < _TTL_SECONDS:
        return bool(_CACHE["value"])
    val = _ping_worker()
    _CACHE["value"] = val
    _CACHE["at"] = now
    return val


def reindex_capability() -> dict:
    eager = _is_eager()
    return {
        "async_mode": not eager,
        "celery_worker_available": celery_worker_available(),
    }
