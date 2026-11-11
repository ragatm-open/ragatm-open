"""Job d'analyse globale de corpus — cycle de vie persistant.

Fondation asynchrone (L2). Le modèle porte tout ce qu'une analyse longue exige :
statut verrouillé, progression, annulation coopérative, idempotence, périmètre
déclaré (pré-vol + empreinte de corpus), résultat et couverture persistants,
comptabilité des coûts, expiration.

Aucun contenu documentaire n'est stocké ici : ni texte d'extrait, ni nom de
fichier. ``error_message`` est un message SÛR pour l'utilisateur — la trace
complète ne vit que dans les logs serveur.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.rag_core.models.rag_space import RagSpace

# `normalize_request_id` tronque à RAG_MAX_REQUEST_ID_CHARS (défaut 200).
# `RagQueryLog.request_id` est un CharField(64) : un en-tête long y lèverait un
# DataError (défaut connu, cf. roadmap TODO 30). On ne reproduit PAS ce bug.
REQUEST_ID_MAX_LENGTH = 200


class AnalysisStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


#: États après lesquels plus rien ne bouge.
TERMINAL_STATUSES: frozenset[str] = frozenset({
    AnalysisStatus.SUCCEEDED,
    AnalysisStatus.PARTIAL,
    AnalysisStatus.FAILED,
    AnalysisStatus.CANCELLED,
})

#: États pour lesquels un job « occupe » encore le couple (espace, type, params).
ACTIVE_STATUSES: frozenset[str] = frozenset({
    AnalysisStatus.PENDING,
    AnalysisStatus.RUNNING,
})

#: Transitions AUTORISÉES. Tout le reste lève. Un état terminal est définitif :
#: on ne relance jamais un job — on en crée un nouveau.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    AnalysisStatus.PENDING: frozenset({
        AnalysisStatus.RUNNING,
        AnalysisStatus.CANCELLED,
        AnalysisStatus.FAILED,
    }),
    AnalysisStatus.RUNNING: frozenset({
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.PARTIAL,
        AnalysisStatus.FAILED,
        AnalysisStatus.CANCELLED,
    }),
    AnalysisStatus.SUCCEEDED: frozenset(),
    AnalysisStatus.PARTIAL: frozenset(),
    AnalysisStatus.FAILED: frozenset(),
    AnalysisStatus.CANCELLED: frozenset(),
}


# Phases de progression. Stables, non sensibles : aucun nom de table, de requête
# SQL ni de technologie n'y transparaît.
PHASE_QUEUED = "queued"
PHASE_COMPUTING_SCOPE = "computing_scope"
PHASE_COMPUTING_STATISTICS = "computing_statistics"
PHASE_PERSISTING_RESULT = "persisting_result"
PHASE_COMPLETED = "completed"
PHASE_CANCELLED = "cancelled"
PHASE_FAILED = "failed"


# Codes d'erreur stables, exposables. Ensemble fermé.
ERROR_UNKNOWN_ANALYSIS_TYPE = "unknown_analysis_type"
ERROR_PREFLIGHT_LIMIT_EXCEEDED = "preflight_limit_exceeded"
ERROR_CANCELLED = "cancelled"
ERROR_SOFT_TIME_LIMIT = "soft_time_limit"
ERROR_HANDLER_ERROR = "handler_error"
ERROR_INVALID_RESULT = "invalid_result"
ERROR_PERMISSION_REVOKED = "permission_revoked"
ERROR_INTERNAL = "internal_error"

# L4 — recherche globale.
ERROR_TOPIC_REQUIRED = "global_search_topic_required"
ERROR_CANDIDATE_LIMIT_EXCEEDED = "candidate_limit_exceeded"
ERROR_UNSOURCED_RESULT = "invalid_unsourced_result"
ERROR_GATEWAY_UNAVAILABLE = "gateway_unavailable"
ERROR_CONCURRENCY_LIMIT = "analytics_concurrency_limit"
ERROR_QUERY_TOO_BROAD = "query_too_broad"

#: Messages SÛRS pour l'utilisateur. Jamais d'exception brute, jamais de trace.
SAFE_ERROR_MESSAGES: dict[str, str] = {
    ERROR_UNKNOWN_ANALYSIS_TYPE: "Ce type d’analyse n’est pas reconnu.",
    ERROR_PREFLIGHT_LIMIT_EXCEEDED: "Le volume à analyser dépasse les limites autorisées.",
    ERROR_CANCELLED: "L’analyse a été annulée.",
    ERROR_SOFT_TIME_LIMIT: "L’analyse a dépassé la durée maximale autorisée.",
    ERROR_HANDLER_ERROR: "L’analyse n’a pas pu être menée à son terme.",
    ERROR_INVALID_RESULT: "L’analyse a produit un résultat inexploitable.",
    ERROR_PERMISSION_REVOKED: "Vous n’avez plus accès à cet espace documentaire.",
    ERROR_INTERNAL: "Une erreur interne est survenue.",
    ERROR_TOPIC_REQUIRED: (
        "Précisez le sujet à rechercher dans l’ensemble des documents."
    ),
    ERROR_CANDIDATE_LIMIT_EXCEEDED: (
        "La recherche a trouvé trop de correspondances pour être traitée sans "
        "réduction du périmètre. Précisez davantage le sujet."
    ),
    ERROR_UNSOURCED_RESULT: (
        "L’analyse a produit des éléments sans source vérifiable ; ils n’ont pas "
        "été retenus."
    ),
    ERROR_GATEWAY_UNAVAILABLE: "Le service d’analyse est momentanément indisponible.",
    ERROR_CONCURRENCY_LIMIT: (
        "Trop d’analyses sont déjà en cours pour cet espace. Réessayez lorsque "
        "l’une d’elles sera terminée."
    ),
    ERROR_QUERY_TOO_BROAD: (
        "La recherche est trop large pour être analysée de manière fiable. "
        "Précisez le sujet ou ajoutez des termes distinctifs."
    ),
}


class InvalidTransition(ValueError):
    """Transition de statut interdite. Jamais rattrapée silencieusement."""


class CorpusAnalysisJob(models.Model):
    """Une analyse globale demandée sur un RagSpace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    rag_space = models.ForeignKey(
        RagSpace, on_delete=models.CASCADE, related_name="analysis_jobs"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="corpus_analysis_jobs",
    )

    # Clé du registre fermé (apps/rag_core/analysis/registry.py). JAMAIS un chemin
    # d'import fourni par le client : le callable n'est pas résolu depuis l'entrée.
    analysis_type = models.CharField(max_length=64)

    parameters = models.JSONField(default=dict, blank=True)
    # sha256 du JSON canonique des paramètres (tri des clés, UTF-8 stable).
    parameters_hash = models.CharField(max_length=64)

    request_id = models.CharField(max_length=REQUEST_ID_MAX_LENGTH, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=AnalysisStatus.choices, default=AnalysisStatus.PENDING
    )
    phase = models.CharField(max_length=32, default=PHASE_QUEUED)
    progress_current = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=1)
    progress_percent = models.PositiveSmallIntegerField(default=0)

    # Périmètre DÉCLARÉ au moment de la création : volumétrie + limites.
    preflight = models.JSONField(default=dict, blank=True)
    # Empreinte du corpus à la création : sert à l'idempotence ET à la péremption.
    corpus_fingerprint = models.CharField(max_length=80, blank=True, default="")

    coverage = models.JSONField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)

    error_code = models.CharField(max_length=64, blank=True, default="")
    # Message SÛR. La trace complète reste dans les logs serveur.
    error_message = models.TextField(blank=True, default="")

    # Lien DURABLE avec le fil de conversation qui a lancé l'analyse. Sans lui, le
    # frontend devrait retrouver le job en parsant le Markdown ou en prenant « le
    # dernier job de l'utilisateur » — deux approches fragiles et fausses.
    # Nullable : une analyse peut être lancée hors chat (API directe).
    chat_thread = models.ForeignKey(
        "chat.ChatThread",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analysis_jobs",
    )
    # Message assistant qui a annoncé le lancement (lien logique, sans FK dure —
    # même convention que ChatMessage.rag_query_log_id).
    result_message_id = models.UUIDField(null=True, blank=True)

    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    tokens_in = models.PositiveBigIntegerField(default=0)
    tokens_out = models.PositiveBigIntegerField(default=0)
    # NULL = coût indéterminé, jamais inventé (même convention que RagQueryLog).
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)

    # Version du schéma de résultat : un résultat produit par une version
    # incompatible ne doit jamais être réutilisé.
    result_version = models.CharField(max_length=16, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_corpus_analysis_job"
        indexes = [
            models.Index(fields=["rag_space", "created_at"]),
            models.Index(fields=["requested_by", "created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["rag_space", "analysis_type", "status"]),
            models.Index(fields=["request_id"]),
            models.Index(fields=["parameters_hash"]),
            models.Index(fields=["corpus_fingerprint"]),
            models.Index(fields=["expires_at"]),
            # Reprise du job après rechargement du fil.
            models.Index(fields=["chat_thread", "-created_at"]),
        ]
        constraints = [
            # Idempotence de requête : un client qui rejoue le même request_id ne
            # crée jamais un second job. Un request_id vide est exclu (partielle).
            models.UniqueConstraint(
                fields=["requested_by", "request_id"],
                condition=~models.Q(request_id=""),
                name="uniq_analysis_job_user_request_id",
            ),
            # Déduplication CONCURRENTE : au plus UN job actif par
            # (espace, type, paramètres, empreinte de corpus). C'est la contrainte
            # partielle qui rend la course impossible — pas un SELECT non verrouillé.
            models.UniqueConstraint(
                fields=["rag_space", "analysis_type", "parameters_hash", "corpus_fingerprint"],
                condition=models.Q(status__in=["pending", "running"]),
                name="uniq_active_analysis_job",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.analysis_type}({self.id}) [{self.status}]"

    # ------------------------------------------------------------------ #
    # État
    # ------------------------------------------------------------------ #

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in ALLOWED_TRANSITIONS.get(self.status, frozenset())

    def transition_to(self, new_status: str) -> None:
        """Change le statut, ou lève. Point de passage UNIQUE.

        Aucun code ne doit assigner ``job.status`` directement : c'est ce qui
        garantit qu'un job terminal ne peut jamais être relancé.
        """
        if not self.can_transition_to(new_status):
            raise InvalidTransition(
                f"transition interdite : {self.status} -> {new_status} (job {self.id})"
            )
        self.status = new_status

    def safe_error_message(self, code: str) -> str:
        return SAFE_ERROR_MESSAGES.get(code, SAFE_ERROR_MESSAGES[ERROR_INTERNAL])
