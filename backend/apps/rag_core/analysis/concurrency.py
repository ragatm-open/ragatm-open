"""Quota de concurrence des analyses coûteuses — ATOMIQUE.

Un `COUNT` suivi d'un `CREATE` sans verrou est une course : deux requêtes
simultanées lisent toutes deux « une place libre » et en créent deux. On prend
donc un **advisory lock PostgreSQL transactionnel**, porté par le workspace : la
lecture et la création se font sous ce verrou, et il est relâché au commit.
"""
from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.db import connection

from apps.rag_core.analysis.registry import AnalysisTypeDefinition
from apps.rag_core.models.corpus_analysis_job import (
    ACTIVE_STATUSES,
    CorpusAnalysisJob,
)

logger = logging.getLogger(__name__)

_LOCK_NAMESPACE = "corpus_analytics_quota"


class ConcurrencyLimitReached(Exception):
    """Le workspace a déjà trop d'analyses coûteuses en cours."""

    def __init__(self, active: int, limit: int) -> None:
        super().__init__("analytics_concurrency_limit")
        self.active = active
        self.limit = limit


def _lock_key(workspace_id) -> int:
    """Clé 64 bits signée, stable, dérivée du workspace."""
    digest = hashlib.sha256(
        f"{_LOCK_NAMESPACE}:{workspace_id}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def acquire_workspace_slot(*, workspace_id, definition: AnalysisTypeDefinition) -> None:
    """Réserve une place, ou lève ``ConcurrencyLimitReached``.

    **À appeler DANS la transaction de création** : le verrou est transactionnel
    (``pg_advisory_xact_lock``), donc il tient jusqu'au commit — la place ne peut
    pas être prise deux fois entre le comptage et l'insertion.

    Les types qui ne consomment pas de quota (instantané SQL) passent sans verrou.
    """
    if not definition.counts_against_concurrency:
        return

    limit = int(getattr(
        settings, "CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE",
        getattr(settings, "CORPUS_ANALYTICS_MAX_CONCURRENT_PER_WORKSPACE", 2),
    ))
    if limit <= 0:
        return

    with connection.cursor() as cur:
        # Sérialise les créations concurrentes du MÊME workspace. Relâché au commit.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", [_lock_key(workspace_id)])

    costly_types = _costly_type_keys()
    active = CorpusAnalysisJob.objects.filter(
        rag_space__workspace_id=workspace_id,
        analysis_type__in=costly_types,
        status__in=list(ACTIVE_STATUSES),
    ).count()

    if active >= limit:
        # On ne crée PAS un job `pending` qu'on ne pourra pas honorer.
        logger.info(
            "corpus_analysis.concurrency_limit workspace_id=%s active=%s limit=%s",
            workspace_id, active, limit,
        )
        raise ConcurrencyLimitReached(active, limit)


def _costly_type_keys() -> list[str]:
    from apps.rag_core.analysis.registry import _REGISTRY

    return [k for k, d in _REGISTRY.items() if d.counts_against_concurrency]
