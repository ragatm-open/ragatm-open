"""Progression et annulation coopérative — service central.

Le pourcentage est TOUJOURS calculé côté serveur : aucun client, aucun handler ne
peut en fournir un. Un job terminal ne bouge plus.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.rag_core.models.corpus_analysis_job import CorpusAnalysisJob

logger = logging.getLogger(__name__)


class AnalysisCancelled(Exception):
    """Annulation demandée : le handler s'arrête au point de contrôle courant."""


def compute_percent(*, current: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, int(round(100.0 * current / total))))


def update_job_progress(job_id, *, phase: str, current: int, total: int) -> None:
    """Met à jour la progression. Invariants appliqués en base, sous verrou.

    - ``current`` et ``total`` >= 0 ; ``current`` <= ``total`` ;
    - le pourcentage est calculé ici, jamais reçu ;
    - **il ne décroît jamais** (une progression qui recule est une régression
      d'affichage : on l'ignore plutôt que de la propager) ;
    - un job terminal est immuable.
    """
    if current < 0 or total < 0:
        raise ValueError(f"progression négative : current={current} total={total}")
    if total > 0 and current > total:
        raise ValueError(f"current ({current}) > total ({total})")

    percent = compute_percent(current=current, total=total)

    with transaction.atomic():
        job = CorpusAnalysisJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None or job.is_terminal:
            return  # terminal : plus rien ne bouge
        if percent < job.progress_percent:
            return  # monotone : jamais de recul
        job.phase = phase
        job.progress_current = current
        job.progress_total = total
        job.progress_percent = percent
        job.save(update_fields=["phase", "progress_current", "progress_total",
                                "progress_percent", "updated_at"])

    logger.info(
        "corpus_analysis.progress analysis_id=%s phase=%s progress_percent=%s",
        job_id, phase, percent,
    )


def raise_if_cancel_requested(job_id) -> None:
    """Point de contrôle d'annulation. À appeler entre deux lots d'un handler.

    L'annulation repose sur le STATUT PERSISTANT, jamais sur ``task.revoke()`` —
    non fiable avec ``acks_late=True``.
    """
    requested = (
        CorpusAnalysisJob.objects.filter(pk=job_id)
        .values_list("cancel_requested_at", flat=True)
        .first()
    )
    if requested is not None:
        raise AnalysisCancelled(str(job_id))


def request_cancel(job) -> str:
    """Demande d'annulation. Idempotent.

    - ``pending``  : rien n'a démarré -> ``cancelled`` immédiat ;
    - ``running``  : marqué ; le handler s'arrête au prochain point de contrôle ;
    - terminal     : sans effet, le résultat n'est jamais modifié.
    """
    from apps.rag_core.models.corpus_analysis_job import (
        ERROR_CANCELLED,
        PHASE_CANCELLED,
        AnalysisStatus,
    )

    with transaction.atomic():
        locked = CorpusAnalysisJob.objects.select_for_update().get(pk=job.pk)

        if locked.is_terminal:
            return locked.status  # idempotent : déjà terminé

        now = timezone.now()
        if locked.cancel_requested_at is None:
            locked.cancel_requested_at = now

        if locked.status == AnalysisStatus.PENDING:
            locked.transition_to(AnalysisStatus.CANCELLED)
            locked.phase = PHASE_CANCELLED
            locked.finished_at = now
            locked.error_code = ERROR_CANCELLED
            locked.error_message = locked.safe_error_message(ERROR_CANCELLED)
            locked.save(update_fields=[
                "status", "phase", "cancel_requested_at", "finished_at",
                "error_code", "error_message", "updated_at",
            ])
            logger.info("corpus_analysis.cancelled analysis_id=%s status=pending", locked.id)
            return locked.status

        locked.save(update_fields=["cancel_requested_at", "updated_at"])

    logger.info("corpus_analysis.cancel_requested analysis_id=%s status=%s", job.pk, locked.status)
    return locked.status
