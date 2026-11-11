"""Purge des jobs d'analyse expirés.

Ne supprime QUE des jobs terminaux et expirés. Un job ``pending`` ou ``running``
n'est jamais supprimé, même si son ``expires_at`` est dépassé : il tourne encore,
et le supprimer laisserait un worker travailler dans le vide.

Ne journalise que des identifiants et des compteurs — jamais le contenu d'un
résultat.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.rag_core.models.corpus_analysis_job import (
    ACTIVE_STATUSES,
    CorpusAnalysisJob,
)

logger = logging.getLogger(__name__)


def purge_expired_analysis_jobs() -> int:
    now = timezone.now()
    qs = CorpusAnalysisJob.objects.filter(
        expires_at__isnull=False,
        expires_at__lte=now,
    ).exclude(status__in=list(ACTIVE_STATUSES))  # jamais un job encore actif

    count = qs.count()
    if count:
        ids = list(qs.values_list("id", flat=True)[:50])
        qs.delete()
        logger.info(
            "corpus_analysis.expired purged=%s sample_ids=%s",
            count, [str(i) for i in ids],
        )
    return count
