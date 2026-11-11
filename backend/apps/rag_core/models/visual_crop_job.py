"""État technique MUTABLE de l'analyse Vision V2 d'un crop.

Une ligne = une unité de travail ``(crop, task, schema_version, context_sha256,
expected_pipeline_fingerprint, analysis_generation)`` — la clé d'idempotence. Le crop
référencé retrouve ``PageRaster → Page → DocumentVersion`` par les relations existantes ;
aucune de ces entités n'est dupliquée ici.

Ce n'est PAS un modèle métier : aucune observation visuelle publiée, aucune citation,
aucun champ ``Chunk.meta``. ``result_raw`` est la réponse V2 déjà validée (K1/K2/K3/K3+),
interne, présente uniquement à l'état ``succeeded``.

Bornes PERSISTÉES (une continuation, un redémarrage ou une redélivrance ne les remet
jamais à zéro) — leur dépassement est refusé par la base elle-même :
  * ``model_attempts`` ≤ ``MAX_MODEL_ATTEMPTS`` : unités de budget modèle réservées AVANT
    chaque POST (remboursées uniquement sur un 503 d'admission observé) ;
  * ``capacity_deferrals`` ≤ ``MAX_CAPACITY_DEFERRALS`` : reports de capacité ;
  * ``activations`` ≤ ``MAX_ACTIVATIONS`` : prises de bail. C'est elle qui borne les POST
    émis par une activation qui meurt sans faire avancer les deux compteurs précédents.
Règles de transition : ``services/raster/orchestration.py``.
"""
import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.rag_core.models.document_version import validate_sha256_hex
from apps.rag_core.models.page_raster import Crop

#: Schéma Vision du rail orchestré (contrat Gateway V2).
VISUAL_JOB_SCHEMA_VERSION = "2"
#: Unités de budget modèle par clé de job (= 1 + 3 relances du rail historique).
MAX_MODEL_ATTEMPTS = 4
#: Reports de capacité par clé de job ; au-delà : ``parked`` (jamais ``terminal_failed``).
MAX_CAPACITY_DEFERRALS = 60
#: Activations (baux pris) par clé de job : 60 reports + 4 tentatives + 8 reprises.
MAX_ACTIVATIONS = MAX_CAPACITY_DEFERRALS + MAX_MODEL_ATTEMPTS + 8


class VisualCropJobState(models.TextChoices):
    PENDING = "pending", "pending"
    RUNNING = "running", "running"
    DEFERRED = "deferred", "deferred"
    SUCCEEDED = "succeeded", "succeeded"
    TERMINAL_FAILED = "terminal_failed", "terminal_failed"
    PARKED = "parked", "parked"
    INELIGIBLE = "ineligible", "ineligible"


#: États dont un job ne sort plus sans nouvelle ``analysis_generation``.
FINAL_STATES = frozenset({VisualCropJobState.SUCCEEDED, VisualCropJobState.TERMINAL_FAILED,
                          VisualCropJobState.PARKED, VisualCropJobState.INELIGIBLE})
#: États qui exigent un code d'erreur explicite.
_ERROR_STATES = [VisualCropJobState.TERMINAL_FAILED, VisualCropJobState.PARKED,
                 VisualCropJobState.INELIGIBLE]


class VisualCropJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Index couvert par la contrainte unique (crop en tête).
    crop = models.ForeignKey(Crop, on_delete=models.CASCADE, related_name="visual_jobs",
                             db_index=False)
    task = models.CharField(max_length=32)
    schema_version = models.CharField(max_length=8, default=VISUAL_JOB_SCHEMA_VERSION)
    #: SHA-256 du contexte canonique envoyé avec le crop (``orchestration.context_sha256``).
    context_sha256 = models.CharField(max_length=64, validators=[validate_sha256_hex])
    #: Empreinte de pipeline V2 attendue (servie par ``/health/vision`` ET allowlistée) ;
    #: elle couvre le digest modèle (contrat CV-3D) — aucune seconde source de vérité.
    expected_pipeline_fingerprint = models.CharField(max_length=128)
    analysis_generation = models.PositiveIntegerField()

    state = models.CharField(max_length=16, choices=VisualCropJobState.choices,
                             default=VisualCropJobState.PENDING)
    model_attempts = models.PositiveIntegerField(default=0)
    capacity_deferrals = models.PositiveIntegerField(default=0)
    activations = models.PositiveIntegerField(default=0)

    #: Une activation antérieure à cet instant ne prend pas le bail (report en cours).
    next_eligible_at = models.DateTimeField(null=True, blank=True)
    #: Dernier envoi d'une activation au broker ; ``NULL`` = hors fenêtre d'enfilement.
    enqueued_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    last_error_code = models.CharField(max_length=64, blank=True, default="")
    #: Réponse V2 validée — INTERNE, uniquement à l'état ``succeeded``.
    result_raw = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_visual_crop_job"
        constraints = [
            models.UniqueConstraint(
                fields=["crop", "task", "schema_version", "context_sha256",
                        "expected_pipeline_fingerprint", "analysis_generation"],
                name="uniq_rag_visual_crop_job_key"),
            models.CheckConstraint(condition=Q(state__in=VisualCropJobState.values),
                                   name="rag_vcj_state_valid"),
            models.CheckConstraint(condition=Q(schema_version=VISUAL_JOB_SCHEMA_VERSION),
                                   name="rag_vcj_schema_version_2"),
            models.CheckConstraint(condition=Q(analysis_generation__gte=1),
                                   name="rag_vcj_generation_gte_1"),
            models.CheckConstraint(
                condition=Q(model_attempts__gte=0) & Q(model_attempts__lte=MAX_MODEL_ATTEMPTS),
                name="rag_vcj_model_attempts_bounded"),
            models.CheckConstraint(
                condition=Q(capacity_deferrals__gte=0)
                & Q(capacity_deferrals__lte=MAX_CAPACITY_DEFERRALS),
                name="rag_vcj_capacity_deferrals_bounded"),
            models.CheckConstraint(
                condition=Q(activations__gte=0) & Q(activations__lte=MAX_ACTIVATIONS),
                name="rag_vcj_activations_bounded"),
            models.CheckConstraint(condition=Q(context_sha256__regex=r"^[0-9a-f]{64}$"),
                                   name="rag_vcj_context_sha256_hex"),
            models.CheckConstraint(condition=~Q(task="") & ~Q(expected_pipeline_fingerprint=""),
                                   name="rag_vcj_task_and_fingerprint_set"),
            # Bail : présent SI ET SEULEMENT SI running.
            models.CheckConstraint(
                condition=(Q(state=VisualCropJobState.RUNNING) & Q(lease_token__isnull=False)
                           & Q(lease_expires_at__isnull=False))
                | (~Q(state=VisualCropJobState.RUNNING) & Q(lease_token__isnull=True)
                   & Q(lease_expires_at__isnull=True)),
                name="rag_vcj_lease_iff_running"),
            # Résultat : présent SI ET SEULEMENT SI succeeded.
            models.CheckConstraint(
                condition=(Q(state=VisualCropJobState.SUCCEEDED) & Q(result_raw__isnull=False))
                | (~Q(state=VisualCropJobState.SUCCEEDED) & Q(result_raw__isnull=True)),
                name="rag_vcj_result_iff_succeeded"),
            models.CheckConstraint(
                condition=~Q(state__in=_ERROR_STATES) | ~Q(last_error_code=""),
                name="rag_vcj_error_code_on_failure"),
        ]
        indexes = [
            # Balayeur (CV-3E4A) : jobs OUVERTS seulement — les jobs terminés n'y entrent
            # jamais, l'index reste petit quel que soit l'historique.
            models.Index(fields=["state", "lease_expires_at", "next_eligible_at", "enqueued_at"],
                         name="rag_vcj_open_idx",
                         condition=Q(state__in=[VisualCropJobState.PENDING, VisualCropJobState.RUNNING,
                                                VisualCropJobState.DEFERRED])),
        ]

    def __str__(self) -> str:
        return f"{self.crop_id}:{self.task}#g{self.analysis_generation}:{self.state}"
