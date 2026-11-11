"""Gestionnaire d'exceptions DRF de l'édition Open (qualification interne, durcissement ciblé).

Objectif unique : qu'une dépendance d'infrastructure indisponible (Redis — cache,
verrous, débit, file Celery — ou le courtier Celery/Kombu) produise une réponse
JSON **explicite et stable** ``503 dependency_unavailable`` au lieu d'une page
d'erreur HTML 500 de Django. Constaté en qualification interne : un dépôt de document
avec Redis arrêté répondait 500 brut avant même le pré-contrôle du courtier.

Règles :
* toute exception que DRF sait déjà traiter (``APIException``, ``Http404``,
  ``PermissionDenied``) garde son traitement par défaut ;
* seules les erreurs de CONNEXION/DÉLAI vers Redis ou le courtier sont converties ;
  une erreur de données ou de programmation reste un 500 (fail closed, visible) ;
* aucun détail interne (hôte, port, message) ne sort ; le ``request_id`` est repris
  de l'en-tête ``X-Request-ID`` posé par ``RequestIdMiddleware`` pour corréler avec
  le journal serveur, qui conserve la trace complète.
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

CODE_DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
RETRY_AFTER_SECONDS = 10


def _redis_connection_errors() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = []
    try:  # redis-py (client direct, django-redis, kombu transport redis)
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        types += [RedisConnectionError, RedisTimeoutError]
    except Exception:  # pragma: no cover - redis toujours présent dans l'édition Open
        pass
    return tuple(types)


def _broker_errors() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = []
    try:
        from kombu.exceptions import OperationalError as KombuOperationalError

        types.append(KombuOperationalError)
    except Exception:  # pragma: no cover
        pass
    return tuple(types)


def classify_dependency(exc: BaseException) -> str | None:
    """Rend le nom stable de la dépendance en panne, ou ``None``."""
    redis_errors = _redis_connection_errors()
    broker_errors = _broker_errors()
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and depth < 6:
        if redis_errors and isinstance(cur, redis_errors):
            return "redis"
        if broker_errors and isinstance(cur, broker_errors):
            return "broker"
        cur = cur.__cause__ or cur.__context__
        depth += 1
    return None


def open_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        return response
    dependency = classify_dependency(exc)
    if dependency is None:
        return None  # => traitement Django par défaut (500), inchangé
    request = context.get("request")
    request_id = None
    if request is not None:
        request_id = getattr(request, "request_id", None) or request.headers.get("x-request-id") or None
    logger.error(
        "dependency_unavailable dependency=%s view=%s request_id=%s error=%s",
        dependency,
        type(context.get("view")).__name__,
        request_id,
        type(exc).__name__,
    )
    resp = Response(
        {
            "detail": CODE_DEPENDENCY_UNAVAILABLE,
            "dependency": dependency,
            "request_id": request_id,
            "retryable": True,
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
    resp["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return resp
