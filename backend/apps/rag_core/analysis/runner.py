"""Exécution d'un job : verrouillage, garde de double exécution, terminaison.

Le worker verrouille le job (``select_for_update``) avant toute décision : deux
exécutions du même ``job_id`` ne produisent jamais deux traitements.
"""
from __future__ import annotations

import logging
import time

from django.db import transaction
from django.utils import timezone

from apps.rag_core.analysis.global_search import GlobalSearchError
from apps.rag_core.analysis.progress import AnalysisCancelled
from apps.rag_core.analysis.registry import UnknownAnalysisType, get_analysis_type
from apps.rag_core.models.corpus_analysis_job import (
    ERROR_CANCELLED,
    ERROR_HANDLER_ERROR,
    ERROR_INVALID_RESULT,
    ERROR_SOFT_TIME_LIMIT,
    ERROR_UNKNOWN_ANALYSIS_TYPE,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_FAILED,
    AnalysisStatus,
    CorpusAnalysisJob,
)

logger = logging.getLogger(__name__)

try:  # Celery peut être absent (imports Django purs / tests unitaires)
    from celery.exceptions import SoftTimeLimitExceeded as SoftTimeLimitExceededProxy
except Exception:  # pragma: no cover
    class SoftTimeLimitExceededProxy(Exception):  # type: ignore[no-redef]
        pass


def _finish(job: CorpusAnalysisJob, status: str, *, phase: str,
            error_code: str = "", extra_fields: dict | None = None) -> None:
    fields = ["status", "phase", "finished_at", "error_code", "error_message", "updated_at"]
    with transaction.atomic():
        locked = CorpusAnalysisJob.objects.select_for_update().get(pk=job.pk)
        if locked.is_terminal:
            return
        locked.transition_to(status)  # point de passage UNIQUE, valide la transition
        locked.phase = phase
        locked.finished_at = timezone.now()
        locked.error_code = error_code
        locked.error_message = locked.safe_error_message(error_code) if error_code else ""
        for k, v in (extra_fields or {}).items():
            setattr(locked, k, v)
            if k not in fields:
                fields.append(k)
        locked.save(update_fields=fields)


def execute_job(job_id) -> dict:
    """Exécute un job. Ne lève jamais : toute issue est un statut persistant."""
    started = time.perf_counter()

    # --- Verrouillage + garde de double exécution -------------------------- #
    with transaction.atomic():
        job = (
            CorpusAnalysisJob.objects.select_for_update()
            .select_related("rag_space")
            .filter(pk=job_id)
            .first()
        )
        if job is None:
            logger.warning("corpus_analysis.missing analysis_id=%s", job_id)
            return {"ok": False, "reason": "missing"}

        if job.is_terminal:
            # Rejeu d'une tâche déjà traitée : on sort proprement, sans rien toucher.
            return {"ok": True, "reason": "already_terminal", "status": job.status}

        if job.status == AnalysisStatus.RUNNING:
            # Déjà en cours ailleurs : jamais deux traitements du même job.
            return {"ok": True, "reason": "already_running"}

        if job.cancel_requested:
            job.transition_to(AnalysisStatus.CANCELLED)
            job.phase = PHASE_CANCELLED
            job.finished_at = timezone.now()
            job.error_code = ERROR_CANCELLED
            job.error_message = job.safe_error_message(ERROR_CANCELLED)
            job.save(update_fields=["status", "phase", "finished_at", "error_code",
                                    "error_message", "updated_at"])
            logger.info("corpus_analysis.cancelled analysis_id=%s", job.id)
            return {"ok": True, "reason": "cancelled"}

        job.transition_to(AnalysisStatus.RUNNING)
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])

    logger.info(
        "corpus_analysis.started analysis_id=%s rag_space_id=%s analysis_type=%s",
        job.id, job.rag_space_id, job.analysis_type,
    )

    # --- Exécution --------------------------------------------------------- #
    try:
        definition = get_analysis_type(job.analysis_type)
    except UnknownAnalysisType:
        _finish(job, AnalysisStatus.FAILED, phase=PHASE_FAILED,
                error_code=ERROR_UNKNOWN_ANALYSIS_TYPE)
        logger.error("corpus_analysis.failed analysis_id=%s error_code=%s",
                     job.id, ERROR_UNKNOWN_ANALYSIS_TYPE)
        return {"ok": False, "error_code": ERROR_UNKNOWN_ANALYSIS_TYPE}

    try:
        out = definition.handler(job=job, rag_space=job.rag_space)

        if not isinstance(out, dict) or "result" not in out:
            _finish(job, AnalysisStatus.FAILED, phase=PHASE_FAILED,
                    error_code=ERROR_INVALID_RESULT)
            return {"ok": False, "error_code": ERROR_INVALID_RESULT}

        total = int(out.get("progress_total") or definition.default_progress_total or 1)
        # Un lot en échec produit `partial`, JAMAIS `succeeded` : un résultat
        # amputé ne doit pas être présenté comme complet.
        final_status = (
            AnalysisStatus.PARTIAL if out.get("partial") else AnalysisStatus.SUCCEEDED
        )
        _finish(
            job, final_status, phase=PHASE_COMPLETED,
            extra_fields={
                "result": out["result"],
                "coverage": out.get("coverage"),
                "corpus_fingerprint": out.get("corpus_fingerprint") or job.corpus_fingerprint,
                "progress_current": total,
                "progress_total": total,
                "progress_percent": 100,
                "tokens_in": int(out.get("tokens_in") or 0),
                "tokens_out": int(out.get("tokens_out") or 0),
                "cost_usd": out.get("cost_usd"),
            },
        )
        duration = time.perf_counter() - started
        logger.info(
            "corpus_analysis.%s analysis_id=%s analysis_type=%s progress_percent=100 "
            "duration_seconds=%.2f tokens_in=%s tokens_out=%s",
            "partial" if out.get("partial") else "completed",
            job.id, job.analysis_type, duration,
            int(out.get("tokens_in") or 0), int(out.get("tokens_out") or 0),
        )
        return {"ok": True, "status": final_status}

    except GlobalSearchError as e:
        # Erreurs métier à CODE STABLE (sujet requis, trop de candidats, résultat
        # non sourcé). Elles ne sont pas des bugs : le message reste sûr.
        _finish(job, AnalysisStatus.FAILED, phase=PHASE_FAILED, error_code=e.code)
        logger.info("corpus_analysis.failed analysis_id=%s error_code=%s", job.id, e.code)
        return {"ok": False, "error_code": e.code}

    except AnalysisCancelled:
        # Annulation coopérative : le handler s'est arrêté à un point de contrôle.
        _finish(job, AnalysisStatus.CANCELLED, phase=PHASE_CANCELLED,
                error_code=ERROR_CANCELLED)
        logger.info("corpus_analysis.cancelled analysis_id=%s", job.id)
        return {"ok": True, "status": AnalysisStatus.CANCELLED}

    except SoftTimeLimitExceededProxy:  # pragma: no cover - dépend de Celery
        _finish(job, AnalysisStatus.FAILED, phase=PHASE_FAILED,
                error_code=ERROR_SOFT_TIME_LIMIT)
        logger.error("corpus_analysis.failed analysis_id=%s error_code=%s",
                     job.id, ERROR_SOFT_TIME_LIMIT)
        return {"ok": False, "error_code": ERROR_SOFT_TIME_LIMIT}

    except Exception:
        # La TRACE va dans les logs serveur (niveau error). Le champ exposé à
        # l'utilisateur ne reçoit qu'un message neutre — jamais l'exception brute.
        logger.exception(
            "corpus_analysis.failed analysis_id=%s analysis_type=%s error_code=%s",
            job.id, job.analysis_type, ERROR_HANDLER_ERROR,
        )
        _finish(job, AnalysisStatus.FAILED, phase=PHASE_FAILED,
                error_code=ERROR_HANDLER_ERROR)
        return {"ok": False, "error_code": ERROR_HANDLER_ERROR}
