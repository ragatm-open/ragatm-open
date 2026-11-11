"""Orchestration Vision V2 des crops — ÉTEINTE PAR DÉFAUT.

    DocumentVersion → Page → PageRaster → Crop ragatm-crop/2 → VisualCropJob
        → GET /v1/vision/capacity (consultatif) → POST /v1/vision/analyze (V2, autorité)
        → résultat V2 validé (K1/K2/K3/K3+), INTERNE (``VisualCropJob.result_raw``)

Portes (toutes requises, évaluées AVANT tout travail) :
  * ``VISUAL_V2_ORCHESTRATION_ENABLED`` (défaut faux) — faux : zéro tâche, zéro réseau ;
  * ``DJANGO_ENV`` de production → refus SAUF ``VISUAL_V2_PRODUCTION_ALLOWED`` (défaut
    faux) : le drapeau générique ne suffit jamais en production ;
  * ``VISUAL_V2_RAGSPACE_ALLOWLIST`` (défaut vide = AUCUN espace) — borne EXTERNE de
    déploiement ;
  * ``VisualScopePolicy`` (CV-3E4A) en base : ``enabled`` requis (ligne absente =
    FERMÉ), ``mode = auto`` requis pour tout déclenchement automatique. Relue à CHAQUE
    tentative HTTP : la révocation est immédiate, sans redémarrage ;
  * empreinte attendue ∈ ``AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS`` ;
  * préflight ``/health/vision`` : schéma ``"2"`` servi pour la tâche, empreinte servie ∈
    ``AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS`` (vide = rail inactif). Pas de relance.

Propriété des relances (aucune autre couche n'en ajoute) :
  * HTTP : ``raster.vision.analyze_crop`` (≤ 4 appels par activation, ``Retry-After``) ;
  * budget modèle : ``VisualCropJob.model_attempts`` ≤ 4, réservé et COMMITÉ avant chaque
    POST (crochet ``before_attempt``), rendu UNIQUEMENT sur un 503 ``vision_overloaded``
    observé ; un 200, un 504, un timeout, une coupure ou un crash le laissent consommé ;
  * capacité : ``capacity_deferrals`` ≤ 60 (puis ``parked``), report = transition persistée
    + ``apply_async(countdown)`` avec gigue ``U(0.8, 1.3) × retry_after`` (≤ 120 s) ;
  * activations : ``activations`` ≤ 72 (puis ``parked``) — la borne qui tient même si une
    activation meurt sans faire avancer les deux compteurs précédents ;
  * Celery : AUCUNE (``max_retries=0``, jamais ``self.retry()``).

Borne globale par clé de job : ``MAX_ACTIVATIONS × MAX_HTTP_ATTEMPTS`` = 72 × 4 = 288 POST
émis au plus, dont au plus 4 consomment une inférence (``MAX_MODEL_ATTEMPTS``).

Transactions : courtes, limitées au claim, aux compteurs, aux transitions et à la
publication. Jamais pendant un rendu, une lecture de fichier, une sonde ou un POST.
Garantie : AU MOINS UNE FOIS, budget persistant (un POST réussi suivi d'un crash avant
publication peut être rejoué ; il reste compté).

Déclenchement automatique : UN seul propriétaire,
``persist_auto_prepare``, appelé DANS la transaction de publication READY
(``indexing._publish_candidate``) ; l'envoi au broker part en ``on_commit``, le balayeur
rattrape un envoi perdu. Aucun déclencheur post-READY hors transaction.

Le succès texte ne dépend jamais de ce module : aucune fonction ici n'écrit
``Document.status`` ni ``Chunk``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Dict, List, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.llm_router.gateway import vision_probe
from apps.llm_router.gateway import vision_v2 as v2
from apps.rag_core.models import (
    Crop,
    DocumentVersion,
    Page,
    PageRaster,
    VisualCropJob,
    VisualPrepareRequestKind,
    VisualPrepareRun,
    VisualPrepareRunState as PR,
    VisualScopeMode,
    VisualScopePolicy,
)
from apps.rag_core.models.visual_policy import PREPARE_FINAL_STATES
from apps.rag_core.models.visual_crop_job import (
    FINAL_STATES,
    MAX_ACTIVATIONS,
    MAX_CAPACITY_DEFERRALS,
    MAX_MODEL_ATTEMPTS,
    VISUAL_JOB_SCHEMA_VERSION,
    VisualCropJobState as S,
)
from apps.rag_core.services.ingest.visual.contracts import VisualContext, logical_model
from apps.rag_core.services.raster import contract
from apps.rag_core.services.raster import vision as V
from apps.rag_core.services.raster.crops import CROP_CONTRACT_VERSION, pad_bbox
from apps.rag_core.services.raster.geometry import VISUAL_CROP_PADDING_PX, project_region_to_raster

logger = logging.getLogger("apps.rag_core.visual_v2")

# -- constantes du contrat d'orchestration --------------------------------------------
TASK = V.DEFAULT_TASK
LEASE_SECONDS = 660                      # > limite dure Celery (600 s)
WINDOW_SIZE = 4                          # jobs enfilés/en cours par version × génération
JITTER_LOW, JITTER_HIGH = 0.8, 1.3
MIN_DEFER_DELAY_S = 1.0
MAX_DEFER_DELAY_S = 120.0
DEFAULT_RETRY_AFTER_S = 10.0             # sonde saturée sans retry_after_seconds
CLEANUP_MARGIN_S = 30.0                  # marge de transition avant la limite souple
TIME_BUDGET_REQUEUE_S = 5
PENDING_STALE_S = 600                    # message perdu : pending enfilé sans claim depuis 10 min
DEFERRED_OVERDUE_S = 300                 # report dont le countdown est perdu (échéance + 5 min)
PREPARE_STALE_S = 600                    # préparation inactive depuis 10 min
LEASE_MARGIN_S = 30                      # bail restant exigé : timeout client + 30 s
SWEEP_MAX_SENDS = 50
PRODUCTION_ENVS = frozenset({"prod", "production"})
DETECTOR_ID = "pdf-embedded-image/1"
ORIGIN_REASON = "pdf_embedded_image"
MAX_HTTP_ATTEMPTS = V.MAX_HTTP_ATTEMPTS
MAX_POST_DISPATCHES_PER_JOB_GENERATION = MAX_ACTIVATIONS * MAX_HTTP_ATTEMPTS
DETECTOR_FINGERPRINT = hashlib.sha256(json.dumps(
    {"detector": DETECTOR_ID, "projection": "ragatm-projection/1", "padding_px": VISUAL_CROP_PADDING_PX},
    sort_keys=True, separators=(",", ":")).encode()).hexdigest()

# Codes d'état (stables, faible cardinalité).
CODE_CAPACITY_EXHAUSTED = "capacity_deferrals_exhausted"
CODE_ACTIVATIONS_EXHAUSTED = "activation_budget_exhausted"
CODE_MODEL_BUDGET_EXHAUSTED = "model_budget_exhausted"
CODE_FINGERPRINT_MISMATCH = "pipeline_fingerprint_mismatch"
CODE_CONTEXT_MISMATCH = "context_mismatch"
CODE_CROP_NOT_SENDABLE = "crop_not_sendable"
CODE_INTERNAL = "internal_error"
CODE_SOURCE_INTEGRITY = "source_integrity_failure"
CODE_NOT_A_PDF = "not_a_pdf"
ACTOR_SYSTEM = "visual_v2_operator"

# -- métriques (noms et étiquettes FERMÉS ; jamais d'identifiant ni de hash) ------------
METRIC_CROPS = "visual_crops_total"
METRIC_SUCCESS = "visual_success_total"
METRIC_TERMINAL = "visual_terminal_failure_total"
METRIC_CAPACITY_DEFERRED = "visual_capacity_deferred_total"
METRIC_PARKED = "visual_parked_total"
METRIC_503 = "visual_gateway_503_total"
METRIC_504 = "visual_gateway_504_total"
METRIC_INELIGIBLE = "visual_ineligible_total"
METRIC_SWEEP_RUNS = "visual_sweeper_runs_total"
METRIC_SWEEP_JOBS = "visual_sweeper_recovered_jobs_total"
METRIC_SWEEP_PREPARES = "visual_sweeper_recovered_prepares_total"
METRIC_SCOPE_CLOSED = "visual_scope_closed_attempt_total"
SWEEP_JOB_REASONS = ("pending_unenqueued", "pending_lost", "deferred_overdue", "lease_expired")
SWEEP_PREPARE_REASONS = ("pending_lost", "running_stalled")
TERMINAL_CODES = (
    v2.VISION_GATEWAY_TIMEOUT, v2.VISION_CLIENT_TIMEOUT, v2.VISION_GATEWAY_UNAVAILABLE,
    v2.VISION_TRANSPORT_ERROR, v2.VISION_UPSTREAM_ERROR, v2.VISION_REQUEST_INVALID,
    v2.VISION_V2_CONTRACT_ERROR, v2.PROVENANCE_INTEGRITY_FAILURE, v2.PIPELINE_NOT_ALLOWED,
    v2.LOCAL_CROP_INTEGRITY_FAILURE, CODE_FINGERPRINT_MISMATCH, CODE_CONTEXT_MISMATCH,
    CODE_MODEL_BUDGET_EXHAUSTED, "other",
)
INELIGIBLE_REASONS = (V.CROP_ENCODED_OVERSIZE, CODE_CROP_NOT_SENDABLE, "other")


def _series(name: str, label: str, value: str) -> str:
    return f'{name}{{{label}="{value}"}}'


COUNTER_NAMES = (
    METRIC_CROPS, METRIC_SUCCESS, METRIC_CAPACITY_DEFERRED, METRIC_PARKED, METRIC_503, METRIC_504,
    *(_series(METRIC_TERMINAL, "code", c) for c in TERMINAL_CODES),
    *(_series(METRIC_INELIGIBLE, "reason", r) for r in INELIGIBLE_REASONS),
    METRIC_SWEEP_RUNS, METRIC_SCOPE_CLOSED,
    *(_series(METRIC_SWEEP_JOBS, "reason", r) for r in SWEEP_JOB_REASONS),
    *(_series(METRIC_SWEEP_PREPARES, "reason", r) for r in SWEEP_PREPARE_REASONS),
)


def _count(name: str, value: int = 1) -> None:
    try:
        from apps.rag_core.services import ops_counters

        ops_counters.incr(name, value)
    except Exception:  # noqa: BLE001 - l'observabilité ne fait jamais échouer le rail
        pass


def _count_terminal(code: str) -> None:
    _count(_series(METRIC_TERMINAL, "code", code if code in TERMINAL_CODES else "other"))


def _count_ineligible(reason: str) -> None:
    _count(_series(METRIC_INELIGIBLE, "reason", reason if reason in INELIGIBLE_REASONS else "other"))


# -- portes -------------------------------------------------------------------------
@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""


def feature_enabled() -> bool:
    return bool(getattr(settings, "VISUAL_V2_ORCHESTRATION_ENABLED", False))


def raster_dpi() -> int:
    return int(getattr(settings, "VISUAL_V2_RASTER_DPI", 200))


def orchestration_gate(rag_space_id) -> GateDecision:
    """Portes LOCALES (aucun réseau). L'ordre garantit qu'à drapeau éteint rien d'autre
    n'est lu."""
    if not feature_enabled():
        return GateDecision(False, "feature_disabled")
    env = str(getattr(settings, "DJANGO_ENV", "") or "").strip().lower()
    if env in PRODUCTION_ENVS and not bool(getattr(settings, "VISUAL_V2_PRODUCTION_ALLOWED", False)):
        return GateDecision(False, "production_forbidden")
    if raster_dpi() not in contract.SUPPORTED_DPIS:
        return GateDecision(False, "raster_dpi_unsupported")
    allow = {str(x).strip() for x in (getattr(settings, "VISUAL_V2_RAGSPACE_ALLOWLIST", None) or [])
             if str(x).strip()}
    if rag_space_id is None or str(rag_space_id) not in allow:
        return GateDecision(False, "ragspace_not_allowed")
    return GateDecision(True)


def is_production() -> bool:
    return str(getattr(settings, "DJANGO_ENV", "") or "").strip().lower() in PRODUCTION_ENVS


def fingerprint_allowed(fingerprint: Optional[str]) -> bool:
    allowed = {f.strip() for f in (getattr(settings, "AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS", None) or [])
               if f and f.strip()}
    return bool(fingerprint) and fingerprint in allowed


@dataclass(frozen=True)
class EffectivePolicy:
    enabled: bool
    mode: str


def effective_policy(rag_space_id) -> EffectivePolicy:
    """Politique EFFECTIVE d'un espace : ligne absente = fermée, mode explicite."""
    row = (VisualScopePolicy.objects.filter(rag_space_id=rag_space_id)
           .values_list("enabled", "mode").first()) if rag_space_id is not None else None
    if row is None:
        return EffectivePolicy(False, VisualScopeMode.EXPLICIT)
    return EffectivePolicy(bool(row[0]), row[1])


def scope_gate(rag_space_id, *, fingerprint: Optional[str] = None, require_auto: bool = False) -> GateDecision:
    """Porte COMPLÈTE : portes de déploiement (``orchestration_gate``) ∧ empreinte ∧
    politique en base ``enabled`` ∧ (``mode = auto`` pour un déclenchement automatique).
    La politique ne peut que restreindre : elle n'est lue qu'une fois les portes de
    déploiement ouvertes."""
    gate = orchestration_gate(rag_space_id)
    if not gate.allowed:
        return gate
    if fingerprint is not None and not fingerprint_allowed(fingerprint):
        return GateDecision(False, "fingerprint_not_allowed")
    policy = effective_policy(rag_space_id)
    if not policy.enabled:
        return GateDecision(False, "policy_disabled")
    if require_auto and policy.mode != VisualScopeMode.AUTO:
        return GateDecision(False, "policy_explicit_only")
    return GateDecision(True)


def gateway_preflight() -> vision_probe.V2Preflight:
    """``/health/vision`` : V2 servi + empreinte servie ∈ allowlist. UN GET, sans relance."""
    if not bool(getattr(settings, "AI_GATEWAY_ENABLED", False)):
        return vision_probe.V2Preflight(False, reason="gateway_disabled")
    return vision_probe.preflight_v2(
        getattr(settings, "AI_GATEWAY_BASE_URL", "") or "", task=TASK,
        logical_model=logical_model(getattr(settings, "AI_GATEWAY_VISION_MODEL", "") or None),
        allowed_fingerprints=getattr(settings, "AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS", None) or [])


# -- contexte -----------------------------------------------------------------------
def job_context() -> VisualContext:
    """Contexte transmis avec un crop CV-3E1 : VIDE (les visuels intégrés PDF n'en ont
    pas, comme au rail V1). Toujours reconstruit, jamais stocké : ``context_sha256``
    prouve que le contexte envoyé est celui de la clé."""
    return VisualContext()


def context_sha256(context: VisualContext) -> str:
    """SHA-256 du JSON CANONIQUE du contexte EXACT envoyé (``as_request()`` : trois champs
    bornés, ``sort_keys``, séparateurs compacts, UTF-8) — ``VisualContext.fingerprint``."""
    return context.fingerprint()


# -- génération -----------------------------------------------------------------------
def _version_jobs(version_id):
    return VisualCropJob.objects.filter(crop__page_raster__page__document_version_id=version_id)


def current_generation(version_id) -> int:
    """Génération courante (une réindexation des mêmes octets la RÉUTILISE)."""
    return _version_jobs(version_id).aggregate(m=Max("analysis_generation"))["m"] or 1


def allocate_generation(version_id) -> int:
    """Nouvelle génération (retraitement EXPLICITE), sous verrou court de la version.
    Deux retraitements concurrents d'une version sans job convergent sur la même valeur :
    c'est une clé idempotente, jamais un doublon."""
    with transaction.atomic():
        if DocumentVersion.objects.select_for_update().filter(id=version_id).first() is None:
            raise DocumentVersion.DoesNotExist(str(version_id))
        top = _version_jobs(version_id).aggregate(m=Max("analysis_generation"))["m"] or 0
        return top + 1


# -- jobs -----------------------------------------------------------------------------
class LogicalParentNotSendable(ValueError):
    """Crop sans artefact (parent logique) : jamais de job d'appel modèle."""


def is_sendable(crop: Crop) -> bool:
    return bool(crop.crop_sha256) and bool(crop.storage_key)


def sendable_units(crop: Crop) -> List[Crop]:
    """Le crop lui-même s'il est matérialisé, sinon ses tuiles (ordre de lecture)."""
    if is_sendable(crop):
        return [crop]
    return [t for t in crop.tiles.order_by("y_min", "x_min") if is_sendable(t)]


def ensure_job(crop: Crop, *, fingerprint: str, generation: int,
               context: Optional[VisualContext] = None, task: str = TASK) -> tuple:
    """``(job, created)`` — idempotent sur la clé ; concurrent-safe (unicité base)."""
    if not is_sendable(crop):
        raise LogicalParentNotSendable(str(crop.id))
    ctx = context or job_context()
    job, created = VisualCropJob.objects.get_or_create(
        crop=crop, task=task, schema_version=VISUAL_JOB_SCHEMA_VERSION,
        context_sha256=context_sha256(ctx), expected_pipeline_fingerprint=fingerprint,
        analysis_generation=int(generation))
    if created:
        _count(METRIC_CROPS)
    return job, created


# -- statut dérivé ----------------------------------------------------------------------
STATUS_NONE = "none"
STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_PARKED = "parked"


def version_status(version_id, generation: int) -> Dict:
    """Statut DÉRIVÉ des jobs (aucun champ persistant) ; succès partiel admis."""
    counts: Dict[str, int] = {}
    for st in _version_jobs(version_id).filter(analysis_generation=generation).values_list("state", flat=True):
        counts[st] = counts.get(st, 0) + 1
    total = sum(counts.values())
    if not total:
        status = STATUS_NONE
    elif counts.get(S.PENDING, 0) + counts.get(S.RUNNING, 0) + counts.get(S.DEFERRED, 0):
        status = STATUS_IN_PROGRESS
    elif counts.get(S.SUCCEEDED, 0) == total:
        status = STATUS_SUCCEEDED
    elif counts.get(S.SUCCEEDED, 0):
        status = STATUS_PARTIAL
    elif counts.get(S.PARKED, 0):
        status = STATUS_PARKED
    else:
        status = STATUS_FAILED
    return {"status": status, "total": total, "states": counts}


# -- bail -----------------------------------------------------------------------------
class LeaseLost(Exception):
    """Le bail n'appartient plus à cette activation : elle n'écrit plus rien."""


class ModelBudgetExhausted(Exception):
    """Plus aucune unité de budget modèle : aucun POST."""


class ScopeClosed(Exception):
    """Une porte (politique en base, drapeau, allowlist, empreinte) s'est fermée : aucun
    POST, aucune unité consommée ; le job redevient ``pending`` sans ré-enfilement."""


class AttemptDeadline(Exception):
    """Le budget mural de l'activation ne couvre plus une tentative : aucun POST."""


@dataclass
class Claim:
    job: VisualCropJob
    token: uuid.UUID


def _clear_lease(job: VisualCropJob) -> None:
    job.lease_token = None
    job.lease_expires_at = None


def claim_job(job_id, *, now=None) -> tuple:
    """Prise de bail, transaction COURTE (``select_for_update(skip_locked=True)``).

    Réclamable : ``pending``/``deferred`` éligible, ou ``running`` à bail EXPIRÉ (reprise
    après crash : tous les compteurs sont conservés). Rend ``(Claim | None, raison)``.
    """
    now = now or timezone.now()
    with transaction.atomic():
        job = VisualCropJob.objects.select_for_update(skip_locked=True).filter(id=job_id).first()
        if job is None:
            return None, "locked_or_gone"
        if job.state == S.RUNNING:
            if job.lease_expires_at is not None and job.lease_expires_at > now:
                return None, "lease_held"
        elif job.state in (S.PENDING, S.DEFERRED):
            if job.next_eligible_at is not None and job.next_eligible_at > now:
                return None, "not_yet_eligible"
        else:
            return None, "final"
        if job.activations >= MAX_ACTIVATIONS:
            job.state, job.last_error_code = S.PARKED, CODE_ACTIVATIONS_EXHAUSTED
            _clear_lease(job)
            job.enqueued_at = job.next_eligible_at = None
            job.save(update_fields=["state", "last_error_code", "lease_token", "lease_expires_at",
                                    "enqueued_at", "next_eligible_at", "updated_at"])
            _count(METRIC_PARKED)
            _on_final(job)
            return None, "parked"
        reclaimed = job.state == S.RUNNING
        token = uuid.uuid4()
        job.state, job.lease_token = S.RUNNING, token
        job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        job.activations += 1
        job.enqueued_at = job.next_eligible_at = None
        job.save(update_fields=["state", "lease_token", "lease_expires_at", "activations",
                                "enqueued_at", "next_eligible_at", "updated_at"])
    _log(job, "claimed", reclaimed=reclaimed)
    return Claim(job=job, token=token), ""


def _owned(job_id, token, now=None) -> VisualCropJob:
    """Job verrouillé ET encore possédé par ``token`` (dans une transaction ouverte)."""
    now = now or timezone.now()
    job = VisualCropJob.objects.select_for_update().filter(id=job_id).first()
    if (job is None or job.state != S.RUNNING or job.lease_token != token
            or job.lease_expires_at is None or job.lease_expires_at <= now):
        raise LeaseLost(str(job_id))
    return job


@dataclass(frozen=True)
class AttemptContext:
    """Ce que le point de contrôle avant tentative doit revérifier (CV-3E4A)."""

    rag_space_id: object
    fingerprint: str
    client_timeout_s: float
    deadline: float                      # échéance murale de l'activation (horloge ``clock``)
    clock: Callable[[], float]


def _check_attempt(job: VisualCropJob, ctx: AttemptContext, now) -> None:
    """Contrôles 3 à 8 du point de contrôle, sur un job déjà possédé (bail valide)."""
    remaining = (job.lease_expires_at - now).total_seconds()
    if remaining < ctx.client_timeout_s + LEASE_MARGIN_S:                 # 3. bail restant
        raise LeaseLost(f"{job.id}:lease_insufficient")
    if not effective_policy(ctx.rag_space_id).enabled:                    # 4-5. politique en base
        raise ScopeClosed("policy_disabled")
    gate = orchestration_gate(ctx.rag_space_id)                           # 6. portes du processus
    if not gate.allowed:
        raise ScopeClosed(gate.reason)
    if not fingerprint_allowed(ctx.fingerprint):
        raise ScopeClosed("fingerprint_not_allowed")
    if ctx.clock() + ctx.client_timeout_s > ctx.deadline:                 # 7. échéance de la tâche
        raise AttemptDeadline(str(job.id))


def reserve_model_unit(job_id, token, ctx: Optional[AttemptContext] = None) -> int:
    """Point de contrôle AVANT CHAQUE POST, en UNE transaction courte, dans cet ordre :
    verrou du job → état/jeton/échéance du bail → bail restant ≥ timeout + 30 s →
    politique en base → portes du processus → empreinte → échéance de la tâche →
    budget modèle → réservation → COMMIT. Toute porte fermée lève AVANT la réservation
    (aucune unité consommée, aucun POST). Rend le nouveau total."""
    with transaction.atomic():
        now = timezone.now()
        job = _owned(job_id, token, now)                                  # 1-2.
        if ctx is not None:
            _check_attempt(job, ctx, now)
        if job.model_attempts >= MAX_MODEL_ATTEMPTS:                      # 8.
            raise ModelBudgetExhausted(str(job_id))
        job.model_attempts += 1                                           # 9.
        job.save(update_fields=["model_attempts", "updated_at"])
        return job.model_attempts                                         # 10. commit en sortie


def check_before_wait(job_id, token, ctx: AttemptContext) -> None:
    """Contrôle LÉGER avant une attente de relance (lecture seule, sans verrou) : libère
    le worker tôt si la porte ou le bail sont déjà perdus. Ne remplace jamais le point
    de contrôle verrouillé de ``reserve_model_unit``."""
    now = timezone.now()
    job = VisualCropJob.objects.filter(id=job_id).only("id", "state", "lease_token", "lease_expires_at").first()
    if (job is None or job.state != S.RUNNING or job.lease_token != token
            or job.lease_expires_at is None or job.lease_expires_at <= now):
        raise LeaseLost(str(job_id))
    _check_attempt(job, ctx, now)


def refund_model_unit(job_id, token) -> int:
    """Rend l'unité d'un 503 d'admission OBSERVÉ (aucune inférence n'a eu lieu)."""
    with transaction.atomic():
        job = _owned(job_id, token)
        if job.model_attempts > 0:
            job.model_attempts -= 1
            job.save(update_fields=["model_attempts", "updated_at"])
        return job.model_attempts


def defer_delay(retry_after_s: Optional[float], *, rng: Callable[[], float] = random.random) -> float:
    """``retry_after × U(0.8, 1.3)``, borné à [1 s, 120 s]. ``rng`` : U(0, 1) injectable."""
    base = DEFAULT_RETRY_AFTER_S if retry_after_s is None else max(0.0, float(retry_after_s))
    factor = JITTER_LOW + (JITTER_HIGH - JITTER_LOW) * float(rng())
    return min(MAX_DEFER_DELAY_S, max(MIN_DEFER_DELAY_S, base * factor))


def _finish(job_id, token, *, state: str, code: str = "", result_raw=None,
            capacity_deferral: bool = False, countdown: Optional[float] = None) -> VisualCropJob:
    """Transition sous bail (transaction courte) ; libère le bail. Un report ré-enfile
    explicitement une activation future APRÈS commit (le créneau de fenêtre est gardé)."""
    now = timezone.now()
    with transaction.atomic():
        job = _owned(job_id, token, now)
        if capacity_deferral:
            job.capacity_deferrals += 1
            if job.capacity_deferrals >= MAX_CAPACITY_DEFERRALS:
                state, code, countdown = S.PARKED, CODE_CAPACITY_EXHAUSTED, None
        job.state, job.last_error_code = state, code
        job.result_raw = result_raw if state == S.SUCCEEDED else None
        _clear_lease(job)
        if state == S.DEFERRED:
            delay = float(countdown or 0)
            job.next_eligible_at = now + timedelta(seconds=delay)
            job.enqueued_at = now
        else:
            job.next_eligible_at = None
            job.enqueued_at = None
        job.save(update_fields=["state", "last_error_code", "result_raw", "lease_token", "lease_expires_at",
                                "capacity_deferrals", "next_eligible_at", "enqueued_at", "updated_at"])
        if state == S.DEFERRED:
            jid, delay = job.id, float(countdown or 0)
            transaction.on_commit(lambda: dispatch_job(jid, countdown=delay))
        else:
            _on_final(job)
    if capacity_deferral:
        _count(METRIC_CAPACITY_DEFERRED)
    if state == S.PARKED:
        _count(METRIC_PARKED)
    elif state == S.TERMINAL_FAILED:
        _count_terminal(code)
    elif state == S.INELIGIBLE:
        _count_ineligible(code)
    elif state == S.SUCCEEDED:
        _count(METRIC_SUCCESS)
    _log(job, state, code=code)
    return job


def _release(job_id, token, *, code: str = "") -> None:
    """Rend le job ``pending`` HORS fenêtre (porte refermée, configuration retirée) : aucun
    ré-enfilement ; seul un appel explicite (commande, nouvelle préparation) le relance."""
    with transaction.atomic():
        job = _owned(job_id, token)
        job.state, job.last_error_code = S.PENDING, code
        _clear_lease(job)
        job.enqueued_at = job.next_eligible_at = None
        job.save(update_fields=["state", "last_error_code", "lease_token", "lease_expires_at",
                                "enqueued_at", "next_eligible_at", "updated_at"])
    _log(job, "released", code=code)


def publish_success(job_id, token, result: v2.VisionV2Result, *, crop: Crop) -> VisualCropJob:
    """Publication SOUS BAIL : relit le job, vérifie l'état, le jeton, K1, K3 et K3+ ; un
    propriétaire périmé n'écrit rien (``LeaseLost``)."""
    allowed = frozenset(getattr(settings, "AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS", None) or [])
    with transaction.atomic():                         # UNE transaction : relire, vérifier, écrire
        job = _owned(job_id, token)
        if result.source_sha256 != crop.crop_sha256:                              # K1
            code = v2.PROVENANCE_INTEGRITY_FAILURE
        elif result.fingerprint not in allowed:                                   # K3
            code = v2.PIPELINE_NOT_ALLOWED
        elif result.fingerprint != job.expected_pipeline_fingerprint:             # K3+
            code = CODE_FINGERPRINT_MISMATCH
        else:
            return _finish(job_id, token, state=S.SUCCEEDED, result_raw=result.raw)
        return _finish(job_id, token, state=S.TERMINAL_FAILED, code=code)


# -- envois (seuls points de contact avec le broker) --------------------------------------
def dispatch_job(job_id, *, countdown: Optional[float] = None) -> None:
    from apps.rag_core.services.ingest.queues import INGEST_VISUAL_QUEUE
    from apps.rag_core.tasks import analyze_crop_job

    kwargs = {"queue": INGEST_VISUAL_QUEUE}
    if countdown:
        kwargs["countdown"] = max(0, int(round(countdown)))
    analyze_crop_job.apply_async(kwargs={"job_id": str(job_id)}, **kwargs)


def dispatch_prepare(run_id) -> None:
    from apps.rag_core.services.ingest.queues import INGEST_VISUAL_QUEUE
    from apps.rag_core.tasks import visual_v2_prepare_version

    visual_v2_prepare_version.apply_async(kwargs={"run_id": str(run_id)}, queue=INGEST_VISUAL_QUEUE)


def _on_final(job: VisualCropJob) -> None:
    """Un job quitte la fenêtre : le suivant de la même version × génération peut partir."""
    version_id = (Crop.objects.filter(id=job.crop_id)
                  .values_list("page_raster__page__document_version_id", flat=True).first())
    if version_id is not None:
        gen = job.analysis_generation
        transaction.on_commit(lambda: fill_crop_job_window(version_id, gen))


def _stale_reason(job: VisualCropJob, now) -> Optional[str]:
    """Pourquoi un job OUVERT déjà dans la fenêtre doit être ré-émis, ou ``None``.

    Règles (une seule implémentation, partagée par les fins de job, la préparation, la
    reprise opérateur et le balayeur) :
      * ``running`` dont le bail est expiré, non ré-émis depuis ``PENDING_STALE_S`` ;
      * ``pending`` enfilé depuis plus de ``PENDING_STALE_S`` sans claim ;
      * ``deferred`` dont l'échéance est dépassée de ``DEFERRED_OVERDUE_S`` et non ré-émis
        depuis (le chemin normal reste le ``countdown`` Celery).
    """
    resent_recently = job.enqueued_at is not None and job.enqueued_at > now - timedelta(seconds=PENDING_STALE_S)
    if job.state == S.RUNNING:
        if job.lease_expires_at is not None and job.lease_expires_at <= now and not resent_recently:
            return "lease_expired"
        return None
    if job.enqueued_at is None:
        return None
    if job.state == S.PENDING and not resent_recently:
        return "pending_lost"
    if job.state == S.DEFERRED:
        due = job.next_eligible_at or job.enqueued_at
        if due + timedelta(seconds=DEFERRED_OVERDUE_S) <= now and (job.enqueued_at <= due or not resent_recently):
            return "deferred_overdue"
    return None


def fill_crop_job_window(version_id, generation: int, *, now=None, max_sends: Optional[int] = None,
                         reasons: Optional[Dict[str, int]] = None) -> int:
    """Complète la fenêtre (≤ ``WINDOW_SIZE`` jobs enfilés ou en cours) d'une version ×
    génération — idempotente — et ré-émet les envois perdus (``_stale_reason``). Seuls les
    jobs dont l'empreinte attendue est autorisée sont envoyés. ``enqueued_at`` est
    commité AVANT l'envoi ; un doublon reste sans effet (un seul claim gagne).
    ``max_sends`` borne les envois ; ``reasons`` reçoit le décompte par motif."""
    now = now or timezone.now()
    budget = max_sends if max_sends is not None else 10 ** 9
    sends: List[tuple] = []
    with transaction.atomic():
        jobs = [j for j in (
            _version_jobs(version_id).filter(analysis_generation=generation)
            .exclude(state__in=list(FINAL_STATES))
            .select_for_update(of=("self",))
            .order_by("crop__page_raster__page__page_number", "crop__y_min", "crop__x_min",
                      "created_at", "id")) if fingerprint_allowed(j.expected_pipeline_fingerprint)]
        in_window = 0
        for job in jobs:
            if job.state == S.RUNNING or job.enqueued_at is not None:
                in_window += 1
                why = _stale_reason(job, now)
                if why and len(sends) < budget:
                    job.enqueued_at = now
                    job.save(update_fields=["enqueued_at", "updated_at"])
                    sends.append((job.id, why))
        free = max(0, WINDOW_SIZE - in_window)
        for job in jobs:
            if free <= 0 or len(sends) >= budget:
                break
            if job.state in (S.PENDING, S.DEFERRED) and job.enqueued_at is None:
                job.enqueued_at = now
                job.save(update_fields=["enqueued_at", "updated_at"])
                sends.append((job.id, "pending_unenqueued"))
                free -= 1
        if sends:
            transaction.on_commit(lambda: [dispatch_job(jid) for jid, _ in sends])
    if reasons is not None:
        for _, why in sends:
            reasons[why] = reasons.get(why, 0) + 1
    return len(sends)


# -- préparation (page par page) --------------------------------------------------------
@dataclass
class PrepareResult:
    outcome: str
    page_number: Optional[int] = None
    crops: int = 0
    jobs_created: int = 0
    skipped_regions: int = 0
    next_cursor: Optional[int] = None
    stats: Dict = field(default_factory=dict)


def _origin() -> dict:
    return {"detector_id": DETECTOR_ID, "detector_fingerprint": DETECTOR_FINGERPRINT,
            "reason": ORIGIN_REASON}


def _discover(data: bytes) -> Dict[int, list]:
    """Régions d'images intégrées (``pdf_visual`` : ≤ 12 par document, ≤ 4 par page),
    groupées par page, ordre de découverte."""
    from apps.rag_core.services.ingest.extraction import config as xcfg
    from apps.rag_core.services.ingest.extraction.pdf_visual import discover_pdf_visual_assets

    by_page: Dict[int, list] = {}
    for asset in discover_pdf_visual_assets(data, max_pages=xcfg.pdf_max_pages()).assets:
        if asset.source_page and asset.source_bbox:
            by_page.setdefault(int(asset.source_page), []).append(tuple(asset.source_bbox))
    return by_page


def prepare_page_crops(page: Page, regions: list, *, data: bytes, dpi: int) -> tuple:
    """Crops ``ragatm-crop/2`` des régions d'une page — rendu ÉVITÉ quand le raster et
    tous les crops existent déjà (réindexation des mêmes octets). Rend
    ``(crops parents, régions écartées)``."""
    params_sha = contract.params_sha256(contract.rasterizer_params(dpi))
    raster = PageRaster.objects.filter(page=page, rasterizer_id=contract.RASTERIZER_ID,
                                       params_sha256=params_sha).first()
    if raster is not None:
        width, height = raster.width, raster.height
    else:
        width, height = contract.expected_raster_size(page.cropbox, page.rotate, dpi)
    projected, skipped = [], 0
    for rect in regions:
        bbox = project_region_to_raster(rect, cropbox=page.cropbox, rotate=page.rotate, dpi=dpi,
                                        raster_width=width, raster_height=height)
        if bbox is None:
            skipped += 1
        elif bbox not in projected:
            projected.append(bbox)
    if not projected:
        return [], skipped
    if raster is not None:
        found = []
        for bbox in projected:
            x0, y0, x1, y1 = pad_bbox(bbox, VISUAL_CROP_PADDING_PX, width, height)
            crop = Crop.objects.filter(page_raster=raster, x_min=x0, y_min=y0, x_max=x1, y_max=y1,
                                       contract_version=CROP_CONTRACT_VERSION,
                                       origin_reason=ORIGIN_REASON).first()
            if crop is None:
                break
            found.append(crop)
        else:
            return found, skipped                                    # aucun rendu
    from apps.rag_core.services.raster import store

    handle = store.ensure_page_raster(page, dpi=dpi, pdf_bytes=data)
    crops = [store.create_crop(handle, origin=_origin(), detected_bbox=bbox,
                               padding_px=VISUAL_CROP_PADDING_PX) for bbox in projected]
    return crops, skipped


def ensure_prepare_run(version_id, *, fingerprint: str, generation: int, kind: str, requested_by=None,
                       reason: str = "", dispatch: bool = True) -> tuple:
    """Crée ou réutilise la préparation DURABLE ``version × génération × empreinte`` et,
    si elle est encore ouverte, l'envoie au broker — APRÈS le commit de la ligne et de
    son ``enqueued_at`` : aucun envoi n'existe sans trace. Rend ``(run, created, sent)``."""
    with transaction.atomic():
        run, created = VisualPrepareRun.objects.get_or_create(
            document_version_id=version_id, analysis_generation=int(generation),
            expected_pipeline_fingerprint=fingerprint,
            defaults={"requested_kind": kind, "requested_by": requested_by, "reason": (reason or "")[:500]})
        run = VisualPrepareRun.objects.select_for_update().get(id=run.id)
        if run.state in PREPARE_FINAL_STATES or not dispatch:
            return run, created, False
        run.enqueued_at = timezone.now()
        run.save(update_fields=["enqueued_at", "updated_at"])
        rid = run.id
        transaction.on_commit(lambda: dispatch_prepare(rid))
    logger.info("visual_v2 prepare_run version_id=%s generation=%d kind=%s created=%s state=%s cursor=%d",
                version_id, run.analysis_generation, kind, created, run.state, run.next_page_cursor)
    return run, created, True


def _close_run(run_id, *, state: str, code: str = "") -> None:
    VisualPrepareRun.objects.filter(id=run_id).exclude(state__in=list(PREPARE_FINAL_STATES)).update(
        state=state, last_error_code=code, updated_at=timezone.now())


def prepare_version_step(run_id) -> PrepareResult:
    """UNE page à visuels par activation (rendu borné), progression DURABLE.

    Convention du curseur : ``next_page_cursor`` = prochaine page PHYSIQUE (1-based) à
    examiner. La page traitée ici est la première page à visuels ``≥ curseur`` ; le
    curseur passe à ``page + 1`` dans la MÊME transaction que l'``enqueued_at`` de la
    continuation, après la création (idempotente) des crops et jobs. Un crash entre les
    deux retraite au pire la même page, sans effet (mêmes crops, mêmes jobs).

    Politique de pages historiques : PARESSEUSE + CIBLÉE — octets relus et vérifiés contre
    ``content_sha256``, ``Page`` créées/réutilisées seulement pour une version
    explicitement préparée d'un espace éligible. Aucun appel Vision ici.
    """
    from apps.rag_core.services.documents.provenance import ensure_pdf_pages
    from apps.rag_core.services.ingest.extraction.pdf_geometry import read_pdf_page_geometry
    from apps.rag_core.services.raster import store

    with transaction.atomic():
        run = VisualPrepareRun.objects.select_for_update().filter(id=run_id).first()
        if run is None:
            return PrepareResult("run_gone")
        if run.state in PREPARE_FINAL_STATES:
            return PrepareResult(f"final:{run.state}")
        if run.state != PR.RUNNING:
            run.state = PR.RUNNING
            run.save(update_fields=["state", "updated_at"])
    version = DocumentVersion.objects.select_related("document").filter(id=run.document_version_id).first()
    if version is None:
        return PrepareResult("version_gone")
    gate = scope_gate(version.document.rag_space_id, fingerprint=run.expected_pipeline_fingerprint)
    if not gate.allowed:                       # la ligne reste ouverte : reprise si la porte rouvre
        return PrepareResult(f"gate_closed:{gate.reason}")
    gen, fingerprint = run.analysis_generation, run.expected_pipeline_fingerprint
    try:
        data = store.read_version_bytes(version)
    except (store.SourceIntegrityFailure, OSError) as exc:
        logger.warning("visual_v2 prepare source unreadable version_id=%s error=%s", version.id,
                       type(exc).__name__)
        _close_run(run.id, state=PR.FAILED, code=CODE_SOURCE_INTEGRITY)
        return PrepareResult(CODE_SOURCE_INTEGRITY)
    geometry = read_pdf_page_geometry(data)
    if not geometry:
        _close_run(run.id, state=PR.FAILED, code=CODE_NOT_A_PDF)
        return PrepareResult(CODE_NOT_A_PDF)
    pages = {p.page_number: p for p in ensure_pdf_pages(version, geometry)}
    by_page = _discover(data)
    numbers = sorted(n for n in by_page if n in pages and n >= run.next_page_cursor)
    if not numbers:
        _close_run(run.id, state=PR.DONE)
        fill_crop_job_window(version.id, gen)
        return PrepareResult("done")
    number = numbers[0]
    parents, skipped = prepare_page_crops(pages[number], by_page[number], data=data, dpi=raster_dpi())
    created = units = 0
    for parent in parents:
        for unit in sendable_units(parent):                         # parent logique : jamais envoyé
            _, was_created = ensure_job(unit, fingerprint=fingerprint, generation=gen)
            units += 1
            created += int(was_created)
    fill_crop_job_window(version.id, gen)
    more = len(numbers) > 1
    with transaction.atomic():                 # progression durable AVANT toute continuation
        current = VisualPrepareRun.objects.select_for_update().filter(id=run.id).first()
        if current is None or current.state in PREPARE_FINAL_STATES:
            return PrepareResult("run_closed_meanwhile", page_number=number)
        current.next_page_cursor = number + 1
        if more:
            current.enqueued_at = timezone.now()
        else:
            current.state = PR.DONE
        current.save(update_fields=["next_page_cursor", "enqueued_at", "state", "updated_at"])
        if more:
            rid = current.id
            transaction.on_commit(lambda: dispatch_prepare(rid))
    logger.info("visual_v2 prepare version_id=%s page_id=%s generation=%d crops=%d jobs_created=%d "
                "skipped_regions=%d next_cursor=%d", version.id, pages[number].id, gen, units, created,
                skipped, number + 1)
    return PrepareResult("page_prepared", page_number=number, crops=units, jobs_created=created,
                         skipped_regions=skipped, next_cursor=(number + 1) if more else None)


# -- activation d'un job --------------------------------------------------------------
def _task_soft_limit_s() -> float:
    return float(getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 540))


def run_crop_job(job_id, *, client: Optional[v2.VisionV2Client] = None,
                 probe: Optional[Callable[[str], vision_probe.CapacityProbe]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Optional[Callable[[float], None]] = None,
                 rng: Callable[[], float] = random.random) -> str:
    """UNE activation = UN crop : claim, contrôles locaux, UNE sonde, analyse, transition."""
    started = clock()
    # Porte COMPLÈTE avant le claim : une porte fermée ne consomme aucune activation.
    row = (VisualCropJob.objects.filter(id=job_id)
           .values_list("crop__page_raster__page__document_version__document__rag_space_id",
                        "expected_pipeline_fingerprint").first())
    if row is None:
        return "gone"
    gate = scope_gate(row[0], fingerprint=row[1])
    if not gate.allowed:
        return f"gate_closed:{gate.reason}"
    claim, why = claim_job(job_id)
    if claim is None:
        return why
    token, job = claim.token, claim.job
    try:
        return _run_claimed(job, token, client=client, probe=probe, clock=clock, sleep=sleep,
                            rng=rng, started=started)
    except LeaseLost:
        logger.info("visual_v2 lease lost job_id=%s", job_id)
        return "lease_lost"
    except Exception:  # noqa: BLE001 - jamais de job « running » orphelin ; borné par activations
        logger.exception("visual_v2 unexpected error job_id=%s", job_id)
        try:
            _finish(job_id, token, state=S.DEFERRED, code=CODE_INTERNAL, countdown=60)
        except LeaseLost:
            pass
        return "internal_error"


def _scope_closed(job: VisualCropJob, token, reason: str) -> str:
    """Porte fermée AVANT une tentative : aucune unité consommée, job ``pending`` hors
    fenêtre, aucun ré-enfilement (le balayeur l'ignorera tant que la porte reste fermée)."""
    _count(METRIC_SCOPE_CLOSED)
    logger.info("visual_v2 scope closed before attempt job_id=%s reason=%s", job.id, reason)
    _release(job.id, token, code="")
    return f"scope_closed:{reason}"


def _run_claimed(job: VisualCropJob, token, *, client, probe, clock, sleep, rng, started) -> str:
    crop = (Crop.objects.select_related("page_raster__page__document_version__document")
            .filter(id=job.crop_id).first())
    if crop is None:
        return "gone"
    space_id = crop.page_raster.page.document_version.document.rag_space_id
    gate = scope_gate(space_id, fingerprint=job.expected_pipeline_fingerprint)
    if not gate.allowed:
        _release(job.id, token, code="")
        return f"gate_closed:{gate.reason}"
    if not is_sendable(crop):
        _finish(job.id, token, state=S.INELIGIBLE, code=CODE_CROP_NOT_SENDABLE)
        return S.INELIGIBLE
    ctx = job_context()
    if context_sha256(ctx) != job.context_sha256:
        _finish(job.id, token, state=S.TERMINAL_FAILED, code=CODE_CONTEXT_MISMATCH)
        return S.TERMINAL_FAILED
    # K1 local + garde encodée finale AVANT toute sonde et tout budget.
    try:
        V.build_request(crop, V.read_crop_bytes(crop), task=job.task, context=ctx)
    except v2.VisionV2Error as exc:
        state = S.INELIGIBLE if exc.code == V.CROP_ENCODED_OVERSIZE else S.TERMINAL_FAILED
        _finish(job.id, token, state=state, code=exc.code)
        return state
    client = client or v2.VisionV2Client.from_settings()
    try:
        client.ensure_ready()
    except v2.VisionV2Error as exc:                       # configuration retirée : pause sûre
        _release(job.id, token, code=exc.code)
        return "gateway_not_configured"

    # UNE sonde de capacité, consultative.
    cap = (probe or vision_probe.probe_capacity)(client.base_url)
    if cap.decision == vision_probe.CAPACITY_SATURATED:
        _finish(job.id, token, state=S.DEFERRED, code="capacity_saturated", capacity_deferral=True,
                countdown=defer_delay(cap.retry_after_s, rng=rng))
        return "capacity_deferred"

    # Budget mural : ne jamais COMMENCER un POST qui ne tiendrait pas avant la limite souple.
    deadline = started + _task_soft_limit_s() - CLEANUP_MARGIN_S
    remaining = deadline - clock()
    if remaining < client.timeout_s:
        _finish(job.id, token, state=S.DEFERRED, code=job.last_error_code,
                countdown=TIME_BUDGET_REQUEUE_S)
        return "time_budget_requeued"

    last = {"code": job.last_error_code or ""}
    actx = AttemptContext(rag_space_id=space_id, fingerprint=job.expected_pipeline_fingerprint,
                          client_timeout_s=float(client.timeout_s), deadline=deadline, clock=clock)

    def before_attempt(n: int) -> None:
        # Point de contrôle AVANT CHAQUE POST (y compris relances 2 à 4) : porte en base,
        # portes du processus, bail, échéance, budget — puis réservation COMMITÉE.
        total = reserve_model_unit(job.id, token, actx)
        logger.info("visual_v2 attempt job_id=%s crop_id=%s model_attempt=%d", job.id, crop.id, total)

    def before_wait(n: int, wait_s: float) -> None:
        check_before_wait(job.id, token, actx)

    def after_attempt(outcome: V.AttemptOutcome) -> None:
        if outcome.kind == V.ATTEMPT_OVERLOADED:
            _count(METRIC_503)
        elif outcome.kind == V.ATTEMPT_MODEL_TIMEOUT:
            _count(METRIC_504)
        if outcome.code:
            last["code"] = outcome.code
        if not outcome.model_consumed:
            refund_model_unit(job.id, token)              # sous bail ; bail perdu ⇒ reste consommée
        logger.info("visual_v2 attempt_result job_id=%s kind=%s gateway_status=%s code=%s",
                    job.id, outcome.kind, outcome.http_status, outcome.code or "")

    try:
        analysis = V.analyze_crop(crop, task=job.task, context=ctx, client=client, budget_s=remaining,
                                  sleep=sleep, clock=clock, before_attempt=before_attempt,
                                  after_attempt=after_attempt, before_wait=before_wait)
    except ScopeClosed as exc:
        return _scope_closed(job, token, str(exc))
    except AttemptDeadline:
        _finish(job.id, token, state=S.DEFERRED, code=last["code"], countdown=TIME_BUDGET_REQUEUE_S)
        return "time_budget_requeued"
    except ModelBudgetExhausted:
        code = last["code"] or CODE_MODEL_BUDGET_EXHAUSTED
        _finish(job.id, token, state=S.TERMINAL_FAILED, code=code)
        return S.TERMINAL_FAILED
    except v2.VisionV2Error as exc:
        return _settle_error(job, token, exc, rng=rng)
    published = publish_success(job.id, token, analysis.result, crop=crop)
    logger.info("visual_v2 analysed job_id=%s attempts=%d latency_ms=%s state=%s", job.id,
                analysis.attempts, analysis.result.latency_ms, published.state)
    return "analysed" if published.state == S.SUCCEEDED else published.state


def _settle_error(job: VisualCropJob, token, exc: v2.VisionV2Error, *, rng) -> str:
    if exc.code == V.CROP_ENCODED_OVERSIZE:
        _finish(job.id, token, state=S.INELIGIBLE, code=exc.code)
        return S.INELIGIBLE
    if exc.http_status == 503 and exc.gateway_code == "vision_overloaded":
        # Admission refusée jusqu'au bout : report de CAPACITÉ, jamais un échec modèle.
        _finish(job.id, token, state=S.DEFERRED, code=exc.code, capacity_deferral=True,
                countdown=defer_delay(exc.retry_after_s, rng=rng))
        return "capacity_deferred"
    if getattr(exc, "exhausted", False):
        used = VisualCropJob.objects.filter(id=job.id).values_list("model_attempts", flat=True).first() or 0
        if used >= MAX_MODEL_ATTEMPTS:
            _finish(job.id, token, state=S.TERMINAL_FAILED, code=exc.code)
            return S.TERMINAL_FAILED
        # Continuation : MÊME budget persistant (aucun budget neuf).
        wait = V._backoff_s(used)
        if exc.retry_after_s is not None:
            wait = max(wait, exc.retry_after_s)
        _finish(job.id, token, state=S.DEFERRED, code=exc.code, countdown=min(MAX_DEFER_DELAY_S, wait))
        return "continuation"
    _finish(job.id, token, state=S.TERMINAL_FAILED, code=exc.code)
    return S.TERMINAL_FAILED


# -- déclenchement automatique : dans la transaction de publication READY ----------------
@dataclass(frozen=True)
class V2Scope:
    version_id: str
    fingerprint: str


def resolve_ingest_scope(*, doc, version, is_pdf: bool) -> Optional[V2Scope]:
    """Le document entre-t-il EFFECTIVEMENT dans le périmètre V2 AUTOMATIQUE ? Drapeau
    éteint : la première ligne rend ``None`` — aucun accès base, aucun réseau. Espace en
    mode ``explicit`` (ou sans politique) : ``None`` sans réseau, le rail V1 différé reste
    inchangé. Ne lève jamais."""
    if not feature_enabled():
        return None
    try:
        if version is None or not is_pdf:
            return None
        gate = scope_gate(doc.rag_space_id, require_auto=True)
        if not gate.allowed:
            return None
        pre = gateway_preflight()
        if not pre.ok:
            logger.info("visual_v2 preflight refused version_id=%s reason=%s", version.id, pre.reason)
            return None
        return V2Scope(version_id=str(version.id), fingerprint=pre.fingerprint)
    except Exception:  # noqa: BLE001 - la vision ne fait jamais échouer le texte
        logger.exception("visual_v2 scope resolution failed document_id=%s", getattr(doc, "id", None))
        return None


AUTO_PREPARE_REASON = "ready_auto_trigger"


class ReadyTransactionRequired(RuntimeError):
    """Le déclencheur automatique n'existe QUE dans la transaction de publication READY."""


def persist_auto_prepare(scope: Optional[V2Scope]) -> Optional[tuple]:
    """SEUL propriétaire du déclenchement AUTOMATIQUE.

    Appelé par ``indexing._publish_candidate`` DANS la transaction qui publie READY :
    la préparation durable (``requested_kind=auto``, ``state=pending``, curseur 1,
    ``requested_by`` NULL, ``enqueued_at``) est écrite dans la MÊME transaction que les
    fragments et le statut. Rollback ⇒ ni READY ni préparation ; commit ⇒ les deux.
    L'envoi au broker est enregistré en ``on_commit`` : un envoi perdu ou en échec
    laisse une ligne ``pending`` que le balayeur retrouve (``pending_lost``).

    Uniquement de la base : aucun réseau, aucun broker. La décision (portes, politique
    ``auto``, empreinte) a été prise AVANT la transaction par ``resolve_ingest_scope`` :
    c'est cette lecture qui décide de l'événement READY. Une fermeture postérieure
    laisse la ligne en base ; claim, ``before_attempt`` et balayeur l'empêchent de
    produire une inférence tant que la porte reste fermée.

    Idempotent sur ``(version, génération, empreinte)`` : republication, rejeu ou
    publication concurrente convergent sur UNE ligne (contrainte unique +
    ``get_or_create`` sous point de sauvegarde). Une préparation déjà finale n'est
    jamais renvoyée. Rend ``(run, created, enqueue_registered)`` ou ``None``.
    """
    if scope is None:
        return None
    if not transaction.get_connection().in_atomic_block:
        raise ReadyTransactionRequired("persist_auto_prepare exige la transaction de publication READY")
    generation = current_generation(scope.version_id)
    with transaction.atomic():
        run, created = VisualPrepareRun.objects.get_or_create(
            document_version_id=scope.version_id, analysis_generation=int(generation),
            expected_pipeline_fingerprint=scope.fingerprint,
            defaults={"requested_kind": VisualPrepareRequestKind.AUTO, "requested_by": None,
                      "reason": AUTO_PREPARE_REASON})
        run = VisualPrepareRun.objects.select_for_update().get(id=run.id)
        registered = run.state not in PREPARE_FINAL_STATES
        if registered:
            run.enqueued_at = timezone.now()
            run.save(update_fields=["enqueued_at", "updated_at"])
            rid = run.id
            transaction.on_commit(lambda: _enqueue_persisted_prepare(rid))
    logger.info("visual_v2 %s version_id=%s generation=%d kind=%s state=%s cursor=%d enqueue_registered=%s",
                "ready_auto_prepare_persisted" if created else "ready_auto_prepare_reused",
                scope.version_id, run.analysis_generation, run.requested_kind, run.state,
                run.next_page_cursor, registered)
    return run, created, registered


def _enqueue_persisted_prepare(run_id) -> None:
    """Rappel ``on_commit`` : n'envoie qu'une préparation DÉJÀ commitée. Ne lève jamais —
    le texte est publié, un broker indisponible ne le rend pas FAILED ; la ligne
    ``pending`` reste au balayeur."""
    try:
        dispatch_prepare(run_id)
    except Exception as exc:  # noqa: BLE001 - la vision ne fait jamais échouer le texte
        logger.warning("visual_v2 ready_auto_prepare_enqueue_failed run_id=%s cls=%s", run_id,
                       exc.__class__.__name__)


# -- balayeur périodique (CV-3E4A) ---------------------------------------------------------
def sweep(*, now=None, max_sends: int = SWEEP_MAX_SENDS) -> Dict:
    """Filet de récupération : préparations et jobs dont l'envoi est perdu.

    FAIL-CLOSED : drapeau éteint ou production → retour immédiat, AUCUNE requête. Ne
    rouvre jamais un état final (job ``succeeded``/``terminal_failed``/``parked``/
    ``ineligible`` ; préparation ``done``/``failed``/``cancelled``). N'envoie rien pour un
    espace dont la porte est fermée (les lignes restent intactes). Au plus ``max_sends``
    envois ; ``enqueued_at`` toujours commité avant l'envoi.
    """
    if not feature_enabled():
        return {"skipped": "feature_disabled"}
    if is_production() and not bool(getattr(settings, "VISUAL_V2_PRODUCTION_ALLOWED", False)):
        return {"skipped": "production_forbidden"}
    now = now or timezone.now()
    stale = now - timedelta(seconds=PREPARE_STALE_S)
    out = {"prepares": {}, "jobs": {}, "sent": 0, "gate_closed": 0}
    gates: Dict = {}

    def gate_for(space_id, fp=None) -> bool:
        key = (space_id, fp)
        if key not in gates:
            gates[key] = scope_gate(space_id, fingerprint=fp).allowed
        return gates[key]

    # 1. Préparations durables (déclenchement perdu, continuation de page perdue).
    with transaction.atomic():
        runs = list(VisualPrepareRun.objects.select_for_update(skip_locked=True, of=("self",))
                    .filter(state__in=[PR.PENDING, PR.RUNNING], updated_at__lt=stale)
                    .filter(Q(enqueued_at__isnull=True) | Q(enqueued_at__lt=stale))
                    .select_related("document_version__document").order_by("updated_at")[:max_sends])
        sent_runs = []
        for run in runs:
            if not gate_for(run.document_version.document.rag_space_id, run.expected_pipeline_fingerprint):
                out["gate_closed"] += 1
                continue
            run.enqueued_at = now
            run.save(update_fields=["enqueued_at", "updated_at"])
            why = "pending_lost" if run.state == PR.PENDING else "running_stalled"
            out["prepares"][why] = out["prepares"].get(why, 0) + 1
            sent_runs.append(run.id)
        if sent_runs:
            transaction.on_commit(lambda: [dispatch_prepare(rid) for rid in sent_runs])
    out["sent"] += len(sent_runs)

    # 2. Jobs : groupes version × génération ayant un job ouvert récupérable (index partiel).
    candidates = (VisualCropJob.objects
                  .filter(Q(state=S.PENDING, enqueued_at__isnull=True)
                          | Q(state=S.PENDING, enqueued_at__lt=now - timedelta(seconds=PENDING_STALE_S))
                          | Q(state=S.DEFERRED, next_eligible_at__lt=now - timedelta(seconds=DEFERRED_OVERDUE_S))
                          | Q(state=S.RUNNING, lease_expires_at__lt=now))
                  .values_list("crop__page_raster__page__document_version_id", "analysis_generation",
                               "crop__page_raster__page__document_version__document__rag_space_id")
                  .distinct()[:max_sends])
    for version_id, generation, space_id in candidates:
        if out["sent"] >= max_sends:
            break
        if not gate_for(space_id):
            out["gate_closed"] += 1
            continue
        out["sent"] += fill_crop_job_window(version_id, generation, now=now,
                                            max_sends=max_sends - out["sent"], reasons=out["jobs"])
    _count(METRIC_SWEEP_RUNS)
    for why, n in out["prepares"].items():
        _count(_series(METRIC_SWEEP_PREPARES, "reason", why), n)
    for why, n in out["jobs"].items():
        _count(_series(METRIC_SWEEP_JOBS, "reason", why), n)
    logger.info("visual_v2 sweep sent=%d prepares=%s jobs=%s gate_closed=%d", out["sent"],
                out["prepares"], out["jobs"], out["gate_closed"])
    return out


def _log(job: VisualCropJob, transition: str, **extra) -> None:
    logger.info("visual_v2 job_id=%s crop_id=%s generation=%d transition=%s model_attempts=%d "
                "capacity_deferrals=%d activations=%d %s", job.id, job.crop_id, job.analysis_generation,
                transition, job.model_attempts, job.capacity_deferrals, job.activations,
                " ".join(f"{k}={v}" for k, v in extra.items()))
