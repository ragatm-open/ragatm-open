"""Création d'un job d'analyse : validation, pré-vol, idempotence, déduplication.

Toute la logique de « faut-il vraiment créer un nouveau job ? » vit ici, sous
transaction. L'enqueue se fait via ``transaction.on_commit`` : le worker ne peut
pas lire un job non committé.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.rag_core.analysis.concurrency import (
    ConcurrencyLimitReached,
    acquire_workspace_slot,
)
from apps.rag_core.analysis.preflight import compute_preflight
from apps.rag_core.analysis.registry import (
    UnknownAnalysisType,
    get_analysis_type,
)
from apps.rag_core.models.corpus_analysis_job import (
    ACTIVE_STATUSES,
    ERROR_PREFLIGHT_LIMIT_EXCEEDED,
    PHASE_QUEUED,
    REQUEST_ID_MAX_LENGTH,
    AnalysisStatus,
    CorpusAnalysisJob,
)

logger = logging.getLogger(__name__)


class PreflightLimitExceeded(Exception):
    def __init__(self, preflight: dict) -> None:
        super().__init__(ERROR_PREFLIGHT_LIMIT_EXCEEDED)
        self.preflight = preflight


@dataclass(frozen=True)
class CreateAnalysisResult:
    job: CorpusAnalysisJob
    created: bool          # un nouveau job a été inséré
    reused: bool           # un résultat déjà calculé est réutilisé
    joined_active: bool    # une analyse identique était déjà en cours


def canonical_parameters(parameters: dict | None) -> str:
    """JSON canonique : tri des clés, UTF-8 stable, séparateurs fixes.

    Jamais ``str(dict)`` — sa représentation dépend de l'ordre d'insertion et du
    repr Python, donc deux demandes identiques produiraient deux empreintes.
    """
    return json.dumps(
        parameters or {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def parameters_hash(parameters: dict | None) -> str:
    return hashlib.sha256(canonical_parameters(parameters).encode("utf-8")).hexdigest()


def normalize_request_id(request_id: str | None) -> str:
    return (request_id or "").strip()[:REQUEST_ID_MAX_LENGTH]


def _expiry() -> timezone.datetime:
    days = int(getattr(settings, "CORPUS_ANALYTICS_RESULT_TTL_DAYS", 30))
    return timezone.now() + timedelta(days=days)


def _enqueue(job_id) -> None:
    """Poste la tâche sur la queue DÉDIÉE, après commit."""
    from apps.rag_core.tasks import run_corpus_analysis_job

    # `queue=` explicite en plus de CELERY_TASK_ROUTES : défense en profondeur.
    # `.delay()` est proscrit ici — il perdrait le routage explicite.
    run_corpus_analysis_job.apply_async(args=[str(job_id)], queue="analytics")


def create_analysis_job(
    *, user, rag_space, analysis_type: str, parameters: dict | None, request_id: str | None
) -> CreateAnalysisResult:
    """Crée (ou réutilise) un job d'analyse. Lève ``UnknownAnalysisType`` ou
    ``PreflightLimitExceeded``."""
    definition = get_analysis_type(analysis_type)  # registre FERMÉ

    params = parameters or {}
    p_hash = parameters_hash(params)
    rid = normalize_request_id(request_id)

    preflight = compute_preflight(rag_space=rag_space, definition=definition)
    if not preflight["within_limits"]:
        raise PreflightLimitExceeded(preflight)

    fingerprint = preflight["corpus_fingerprint"]

    with transaction.atomic():
        # 1) Idempotence de requête : le même request_id ne crée jamais un 2e job.
        if rid:
            existing = CorpusAnalysisJob.objects.filter(
                requested_by=user, request_id=rid
            ).first()
            if existing is not None:
                return CreateAnalysisResult(
                    job=existing, created=False, reused=False, joined_active=False
                )

        # 2) Résultat déjà calculé et TOUJOURS valable -> réutilisation, zéro dépense.
        #    Jamais un failed/cancelled/partial, jamais un résultat expiré, jamais
        #    une version de résultat incompatible, jamais un corpus différent.
        now = timezone.now()
        reusable = (
            CorpusAnalysisJob.objects.filter(
                rag_space=rag_space,
                analysis_type=analysis_type,
                parameters_hash=p_hash,
                corpus_fingerprint=fingerprint,
                status=AnalysisStatus.SUCCEEDED,
                result_version=definition.result_version,
            )
            .filter(Q(expires_at__gt=now) | Q(expires_at__isnull=True))
            .order_by("-finished_at")
            .first()
        )
        if reusable is not None:
            logger.info(
                "corpus_analysis.reused analysis_id=%s rag_space_id=%s analysis_type=%s",
                reusable.id, rag_space.id, analysis_type,
            )
            return CreateAnalysisResult(
                job=reusable, created=False, reused=True, joined_active=False
            )

        # 3) Quota de concurrence — ATOMIQUE, sous advisory lock transactionnel.
        #    Placé ICI, dans la transaction, APRÈS l'idempotence et la réutilisation
        #    (un job rejoué ou réutilisé ne consomme aucune place) et AVANT la
        #    création : on ne crée jamais un job `pending` qu'on ne pourra honorer.
        acquire_workspace_slot(
            workspace_id=rag_space.workspace_id, definition=definition
        )

        # 4) Création. La contrainte partielle `uniq_active_analysis_job` rend la
        #    course impossible : deux créations concurrentes identiques -> l'une
        #    insère, l'autre reçoit IntegrityError et REJOINT le job existant.
        try:
            with transaction.atomic():  # savepoint : l'IntegrityError n'avorte pas tout
                job = CorpusAnalysisJob.objects.create(
                    rag_space=rag_space,
                    requested_by=user,
                    analysis_type=analysis_type,
                    parameters=params,
                    parameters_hash=p_hash,
                    request_id=rid,
                    status=AnalysisStatus.PENDING,
                    phase=PHASE_QUEUED,
                    progress_total=definition.default_progress_total,
                    preflight=preflight,
                    corpus_fingerprint=fingerprint,
                    result_version=definition.result_version,
                    expires_at=_expiry(),
                )
        except IntegrityError:
            active = (
                CorpusAnalysisJob.objects.filter(
                    rag_space=rag_space,
                    analysis_type=analysis_type,
                    parameters_hash=p_hash,
                    corpus_fingerprint=fingerprint,
                    status__in=list(ACTIVE_STATUSES),
                )
                .order_by("created_at")
                .first()
            )
            if active is None:
                raise
            logger.info("corpus_analysis.joined_active analysis_id=%s", active.id)
            return CreateAnalysisResult(
                job=active, created=False, reused=False, joined_active=True
            )

        # 5) Enqueue APRÈS commit : le worker ne peut pas lire un job non committé.
        transaction.on_commit(lambda: _enqueue(job.id))

    logger.info(
        "corpus_analysis.created analysis_id=%s rag_space_id=%s analysis_type=%s "
        "documents_total=%s chunks_total=%s",
        job.id, rag_space.id, analysis_type,
        preflight["documents_total"], preflight["chunks_total"],
    )
    return CreateAnalysisResult(job=job, created=True, reused=False, joined_active=False)


def is_stale(job: CorpusAnalysisJob) -> bool | None:
    """Le corpus a-t-il changé depuis la génération de ce résultat ?

    ``None`` tant que le job n'est pas terminal : on ne recalcule pas l'empreinte
    à chaque tour de polling d'un job en cours — ce serait du SQL pour rien.
    """
    if not job.is_terminal:
        return None
    from apps.rag_core.services.coverage_service import _fingerprint

    return _fingerprint(job.rag_space_id) != job.corpus_fingerprint


__all__ = [
    "ConcurrencyLimitReached",
    "CreateAnalysisResult",
    "PreflightLimitExceeded",
    "UnknownAnalysisType",
    "canonical_parameters",
    "create_analysis_job",
    "is_stale",
    "normalize_request_id",
    "parameters_hash",
]
