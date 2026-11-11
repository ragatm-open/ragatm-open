"""Réindexation globale d'un RagSpace — jobs DURABLES et reprenables.

Remplace l'ancien chemin synchrone (`RagSpaceReindexView` bouclant sur tous les
documents dans la requête HTTP, qui dépassait le timeout du proxy à 300 s et
laissait un état partiel). Le travail vit désormais dans un job persistant :

- POST → création atomique du job + un item PAR document → réponse 202 immédiate ;
- traitement asynchrone document par document (Celery) ;
- progression persistée, reprise après crash (lease + reap), échecs isolés.

Aucun contenu documentaire n'est stocké ici (ni texte, ni embedding). ``error_message``
est un message SÛR pour l'utilisateur ; la trace complète ne vit que dans les logs.

Conventions alignées sur ``CorpusAnalysisJob`` (transitions verrouillées, contrainte
partielle « un seul job actif », messages d'erreur fermés).
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.rag_core.models.document import Document
from apps.rag_core.models.rag_space import RagSpace


# --------------------------------------------------------------------------- #
# Statuts
# --------------------------------------------------------------------------- #
class ReindexJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUING = "queuing", "Queuing"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors", "Completed with errors"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    INTERRUPTED = "interrupted", "Interrupted"


class ReindexItemStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    INTERRUPTED = "interrupted", "Interrupted"
    #: Retenu dans le snapshot mais VOLONTAIREMENT non traité (échec définitif,
    #: stratégie inapplicable…). Distinct de FAILED : rien n'a été tenté, et rien
    #: ne devait l'être. Les confondre ferait passer une décision pour une panne.
    SKIPPED = "skipped", "Skipped"


#: Un job « occupe » encore le RagSpace (empêche un second job global concurrent).
JOB_ACTIVE_STATUSES: frozenset[str] = frozenset({
    ReindexJobStatus.PENDING,
    ReindexJobStatus.QUEUING,
    ReindexJobStatus.RUNNING,
})

JOB_TERMINAL_STATUSES: frozenset[str] = frozenset({
    ReindexJobStatus.COMPLETED,
    ReindexJobStatus.COMPLETED_WITH_ERRORS,
    ReindexJobStatus.FAILED,
    ReindexJobStatus.CANCELLED,
    ReindexJobStatus.INTERRUPTED,
})

#: Transitions AUTORISÉES du job. Tout le reste lève. Terminal = définitif.
JOB_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    ReindexJobStatus.PENDING: frozenset({
        ReindexJobStatus.QUEUING, ReindexJobStatus.FAILED, ReindexJobStatus.CANCELLED,
    }),
    ReindexJobStatus.QUEUING: frozenset({
        ReindexJobStatus.RUNNING, ReindexJobStatus.FAILED, ReindexJobStatus.CANCELLED,
    }),
    ReindexJobStatus.RUNNING: frozenset({
        ReindexJobStatus.COMPLETED, ReindexJobStatus.COMPLETED_WITH_ERRORS,
        ReindexJobStatus.FAILED, ReindexJobStatus.CANCELLED, ReindexJobStatus.INTERRUPTED,
    }),
    ReindexJobStatus.COMPLETED: frozenset(),
    ReindexJobStatus.COMPLETED_WITH_ERRORS: frozenset(),
    ReindexJobStatus.FAILED: frozenset(),
    ReindexJobStatus.CANCELLED: frozenset(),
    ReindexJobStatus.INTERRUPTED: frozenset(),
}

#: Un item est encore « à faire » (repris par le reap / la reprise).
ITEM_UNFINISHED_STATUSES: frozenset[str] = frozenset({
    ReindexItemStatus.PENDING,
    ReindexItemStatus.QUEUED,
    ReindexItemStatus.RUNNING,
})

ITEM_TERMINAL_STATUSES: frozenset[str] = frozenset({
    ReindexItemStatus.SUCCEEDED,
    ReindexItemStatus.FAILED,
    ReindexItemStatus.CANCELLED,
    ReindexItemStatus.INTERRUPTED,
    ReindexItemStatus.SKIPPED,
})


# Codes d'erreur stables, exposables. Ensemble fermé (jamais d'exception brute).
ERROR_ENQUEUE_FAILED = "enqueue_failed"
ERROR_BROKER_UNAVAILABLE = "reindex_enqueue_unavailable"
ERROR_EAGER_TOO_LARGE = "reindex_eager_too_large"
ERROR_NO_STORAGE_FILE = "no_storage_file_for_reindex"
#: La source est DÉCLARÉE en base mais introuvable dans le stockage. Distinct
#: du précédent : ici le document a bien eu une source, elle n'est plus là.
ERROR_SOURCE_FILE_MISSING = "source_file_missing"
ERROR_EXTRACTION_FAILED = "reindex_extraction_failed"
ERROR_INTERRUPTED = "reindex_interrupted"
ERROR_INTERNAL = "internal_error"

SAFE_ERROR_MESSAGES: dict[str, str] = {
    ERROR_ENQUEUE_FAILED: "La mise en file d’un document a échoué.",
    ERROR_BROKER_UNAVAILABLE: "Le service de réindexation est momentanément indisponible.",
    ERROR_EAGER_TOO_LARGE: (
        "La réindexation globale nécessite un worker asynchrone (indisponible ici "
        "pour ce volume)."
    ),
    ERROR_NO_STORAGE_FILE: "Aucun fichier source n’a été conservé pour ce document.",
    ERROR_SOURCE_FILE_MISSING: (
        "Le fichier source de ce document est introuvable dans le stockage. "
        "Il doit être téléversé à nouveau : réessayer ne le fera pas réapparaître."
    ),
    ERROR_EXTRACTION_FAILED: "La ré-extraction de ce document a échoué.",
    ERROR_INTERRUPTED: "Le traitement de ce document a été interrompu.",
    ERROR_INTERNAL: "Une erreur interne est survenue.",
}


class InvalidReindexTransition(ValueError):
    """Transition de statut interdite. Jamais rattrapée silencieusement."""


class ReindexJobKind(models.TextChoices):
    """Ce qu'un job REPRÉSENTE — sa portée, pas son traitement.

    Le traitement d'un item est rigoureusement identique quel que soit le `kind`
    (même appel à ``reindex_existing_document``, mêmes invariants). Seule change la
    façon dont le **snapshot** de départ a été constitué. Un `kind` qui ferait
    diverger le traitement devrait être une tâche distincte, pas une valeur d'enum.
    """

    #: Tous les documents réindexables de l'espace (bouton « Réindexer »).
    FULL_REINDEX = "full_reindex", "Full reindex"
    #: Reprise ciblée des documents en ERREUR (sélection explicite ou par filtre).
    FAILED_DOCUMENTS_RETRY = "failed_documents_retry", "Failed documents retry"


#: Comment la sélection d'un job de reprise a été exprimée. Purement descriptif :
#: la vérité opérationnelle reste la liste d'items matérialisée au lancement.
SELECTION_MODE_EXPLICIT = "explicit"
SELECTION_MODE_FILTER = "filter"
SELECTION_MODES = (SELECTION_MODE_EXPLICIT, SELECTION_MODE_FILTER)


class RagSpaceReindexJob(models.Model):
    """Une réindexation globale demandée sur un RagSpace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    rag_space = models.ForeignKey(
        RagSpace, on_delete=models.CASCADE, related_name="reindex_jobs"
    )
    # SET_NULL : l'historique du job survit à la suppression de l'utilisateur.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ragspace_reindex_jobs",
    )
    # Lignée de relance : un job « retry-failed » pointe le job d'origine (jamais
    # d'écrasement d'historique).
    parent_job = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="retries"
    )

    status = models.CharField(
        max_length=24, choices=ReindexJobStatus.choices, default=ReindexJobStatus.PENDING
    )

    # Portée du job. Défaut = comportement historique : tout job créé avant ce champ
    # est bien une réindexation globale, la valeur par défaut n'invente rien.
    kind = models.CharField(
        max_length=32, choices=ReindexJobKind.choices, default=ReindexJobKind.FULL_REINDEX
    )

    # Trace de la sélection demandée (mode, terme de recherche, compteurs). Sert au
    # diagnostic et à l'affichage, JAMAIS à décider quoi traiter : les items créés au
    # lancement sont l'unique source de vérité. Un job qui réévaluerait un filtre en
    # cours de route verrait son total bouger sous ses pieds.
    selection_meta = models.JSONField(default=dict, blank=True)

    # Stratégie DEMANDÉE pour la campagne (`standard`, `force_ocr`,
    # `extended_timeout`, `auto`). `auto` n'est pas un traitement : c'est une
    # demande de résolution, tranchée document par document au lancement et figée
    # dans `item.effective_strategy`. Défaut `standard` = comportement historique.
    requested_strategy = models.CharField(max_length=32, default="standard")

    # Compteurs. Invariant : total = pending+queued+running+succeeded+failed+cancelled+interrupted.
    total_count = models.PositiveIntegerField(default=0)
    pending_count = models.PositiveIntegerField(default=0)
    queued_count = models.PositiveIntegerField(default=0)
    running_count = models.PositiveIntegerField(default=0)
    succeeded_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    cancelled_count = models.PositiveIntegerField(default=0)
    interrupted_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)

    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_space_reindex_job"
        indexes = [
            models.Index(fields=["rag_space", "status"]),
            models.Index(fields=["rag_space", "-created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            # Exclusivité : au plus UN job actif par RagSpace. C'est la contrainte
            # partielle qui rend la course impossible (pas un SELECT non verrouillé).
            models.UniqueConstraint(
                fields=["rag_space"],
                condition=models.Q(status__in=["pending", "queuing", "running"]),
                name="uniq_active_reindex_job_per_space",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug
        return f"ReindexJob({self.id}) [{self.status}] {self.rag_space_id}"

    @property
    def is_terminal(self) -> bool:
        return self.status in JOB_TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in JOB_ACTIVE_STATUSES

    @property
    def completed_count(self) -> int:
        """Items dans un état terminal (base du pourcentage).

        Les items sautés en font partie : ils sont décidés, pas en attente. Les
        exclure ferait plafonner la progression sous 100 % sur un lot contenant des
        échecs définitifs, laissant croire à un job qui ne finit pas.
        """
        return (
            self.succeeded_count + self.failed_count + self.cancelled_count
            + self.interrupted_count + self.skipped_count
        )

    @property
    def progress_percent(self) -> float:
        if not self.total_count:
            return 100.0
        return round(100.0 * self.completed_count / self.total_count, 2)

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in JOB_ALLOWED_TRANSITIONS.get(self.status, frozenset())

    def transition_to(self, new_status: str) -> None:
        if not self.can_transition_to(new_status):
            raise InvalidReindexTransition(
                f"transition interdite : {self.status} -> {new_status} (job {self.id})"
            )
        self.status = new_status

    def safe_error_message(self, code: str) -> str:
        return SAFE_ERROR_MESSAGES.get(code, SAFE_ERROR_MESSAGES[ERROR_INTERNAL])


class RagSpaceReindexJobItem(models.Model):
    """Une unité de travail : la réindexation d'UN document dans un job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    job = models.ForeignKey(
        RagSpaceReindexJob, on_delete=models.CASCADE, related_name="items"
    )
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="reindex_items"
    )

    status = models.CharField(
        max_length=16, choices=ReindexItemStatus.choices, default=ReindexItemStatus.PENDING
    )
    attempt_count = models.PositiveIntegerField(default=0)
    celery_task_id = models.CharField(max_length=155, blank=True, default="")

    # Stratégie RÉSOLUE pour CE document, figée au lancement en même temps que le
    # snapshot. Ne vaut jamais `auto` : `auto` est une demande, pas un traitement.
    # Un job en cours ne peut donc pas voir sa stratégie changer sous lui, même si
    # le code d'erreur du document évolue entretemps.
    effective_strategy = models.CharField(max_length=32, default="standard")

    # Trace descriptive de la décision (code d'erreur ayant motivé la stratégie,
    # retryabilité, budget de temps appliqué). Jamais de contenu documentaire.
    strategy_meta = models.JSONField(default=dict, blank=True)

    # Raison STRUCTURÉE d'un item `skipped` (`non_retryable`,
    # `replace_source_recommended`, `strategy_not_supported`). Vide sinon.
    skip_reason = models.CharField(max_length=48, blank=True, default="")

    # Lease : détecte les items dont le worker est mort (RUNNING sans heartbeat frais).
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_space_reindex_job_item"
        indexes = [
            models.Index(fields=["job", "status"]),
            models.Index(fields=["document", "status"]),
            models.Index(fields=["status", "lease_expires_at"]),
        ]
        constraints = [
            # Un seul item par document et par job (idempotence de création).
            models.UniqueConstraint(
                fields=["job", "document"], name="uniq_reindex_item_job_document"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug
        return f"ReindexItem({self.id}) [{self.status}] doc={self.document_id}"

    @property
    def is_terminal(self) -> bool:
        return self.status in ITEM_TERMINAL_STATUSES
