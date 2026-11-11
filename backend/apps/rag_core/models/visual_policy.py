"""Politique de périmètre Vision V2 et préparations durables.

``VisualScopePolicy`` — UNE ligne au plus par RagSpace, écrite par l'opérateur
(``visual_v2_scope``), jamais par une migration. Elle S'AJOUTE aux portes de
déploiement (drapeau, garde de production, allowlist d'environnement, empreinte) :
elle ne peut que restreindre, jamais élargir. Absence de ligne = FERMÉ
(``enabled`` faux, mode ``explicit``). C'est aussi le levier de révocation
IMMÉDIATE (sans redémarrage) : chaque tentative HTTP la relit.

``VisualPrepareRun`` — trace DURABLE d'une préparation ``version × génération ×
empreinte`` : qui l'a demandée, où elle en est (``next_page_cursor``), quand elle a
été envoyée au broker. Écrite et commitée AVANT tout envoi : un déclenchement ou
une continuation perdus restent retrouvables par le balayeur.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.rag_core.models.document_version import DocumentVersion
from apps.rag_core.models.rag_space import RagSpace


class VisualScopeMode(models.TextChoices):
    #: Seule ``visual_v2_prepare`` explicite démarre la V2 ; ingestion et réindexation
    #: ne la déclenchent jamais (le rail V1 différé reste inchangé).
    EXPLICIT = "explicit", "explicit"
    #: Comportement CV-3E2/3E3 : READY → préparation V2 automatique, V1 différé évincé.
    AUTO = "auto", "auto"


class VisualScopePolicy(models.Model):
    id = models.BigAutoField(primary_key=True)
    rag_space = models.OneToOneField(RagSpace, on_delete=models.CASCADE, related_name="visual_scope_policy")
    mode = models.CharField(max_length=16, choices=VisualScopeMode.choices, default=VisualScopeMode.EXPLICIT)
    enabled = models.BooleanField(default=False)
    reason = models.CharField(max_length=500)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_visual_scope_policy"
        constraints = [
            models.CheckConstraint(condition=Q(mode__in=VisualScopeMode.values), name="rag_vsp_mode_valid"),
            models.CheckConstraint(condition=~Q(reason=""), name="rag_vsp_reason_set"),
        ]

    def __str__(self) -> str:
        return f"{self.rag_space_id}:{self.mode}:{'on' if self.enabled else 'off'}"


class VisualPrepareRunState(models.TextChoices):
    PENDING = "pending", "pending"
    RUNNING = "running", "running"
    DONE = "done", "done"
    FAILED = "failed", "failed"
    CANCELLED = "cancelled", "cancelled"


#: États qu'aucun mécanisme automatique ne relance.
PREPARE_FINAL_STATES = frozenset({VisualPrepareRunState.DONE, VisualPrepareRunState.FAILED,
                                  VisualPrepareRunState.CANCELLED})


class VisualPrepareRequestKind(models.TextChoices):
    AUTO = "auto", "auto"
    OPERATOR = "operator", "operator"
    SWEEPER = "sweeper", "sweeper"


class VisualPrepareRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Index couvert par la contrainte unique (version en tête).
    document_version = models.ForeignKey(DocumentVersion, on_delete=models.CASCADE,
                                         related_name="visual_prepare_runs", db_index=False)
    analysis_generation = models.PositiveIntegerField()
    expected_pipeline_fingerprint = models.CharField(max_length=128)
    state = models.CharField(max_length=16, choices=VisualPrepareRunState.choices,
                             default=VisualPrepareRunState.PENDING)
    #: Prochaine page PHYSIQUE (1-based) à examiner. Une page n'est comptée préparée
    #: qu'une fois ce curseur commité APRÈS elle ; une reprise retraite au pire la page
    #: en cours, de façon idempotente.
    next_page_cursor = models.PositiveIntegerField(default=1)
    requested_kind = models.CharField(max_length=16, choices=VisualPrepareRequestKind.choices)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    reason = models.CharField(max_length=500, blank=True, default="")
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    #: Dernier envoi au broker (commité AVANT l'envoi).
    enqueued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_visual_prepare_run"
        constraints = [
            models.UniqueConstraint(fields=["document_version", "analysis_generation",
                                            "expected_pipeline_fingerprint"],
                                    name="uniq_rag_visual_prepare_run_key"),
            models.CheckConstraint(condition=Q(state__in=VisualPrepareRunState.values), name="rag_vpr_state_valid"),
            models.CheckConstraint(condition=Q(requested_kind__in=VisualPrepareRequestKind.values),
                                   name="rag_vpr_kind_valid"),
            models.CheckConstraint(condition=Q(analysis_generation__gte=1), name="rag_vpr_generation_gte_1"),
            models.CheckConstraint(condition=Q(next_page_cursor__gte=1), name="rag_vpr_cursor_gte_1"),
            models.CheckConstraint(condition=~Q(expected_pipeline_fingerprint=""), name="rag_vpr_fingerprint_set"),
        ]
        indexes = [models.Index(fields=["state", "updated_at"], name="rag_vpr_state_updated_idx")]

    def __str__(self) -> str:
        return f"{self.document_version_id}#g{self.analysis_generation}:{self.state}@{self.next_page_cursor}"
