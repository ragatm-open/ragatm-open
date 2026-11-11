from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class RagQueryLog(models.Model):
    """Audit log d'une requête RAG.

    Garanties :
    - 1 requête = 1 log
    - statut final : ok | no_answer | error
    - error_stage renseigné uniquement si status=error
    - sources respecte un contrat public versionné
    """

    # --- Status ---
    STATUS_OK = "ok"
    STATUS_NO_ANSWER = "no_answer"
    STATUS_ERROR = "error"

    STATUS_CHOICES = (
        (STATUS_OK, "OK"),
        (STATUS_NO_ANSWER, "No answer"),
        (STATUS_ERROR, "Error"),
    )

    # --- Error stage ---
    ERROR_STAGE_RETRIEVAL = "retrieval"
    ERROR_STAGE_LLM = "llm"

    ERROR_STAGE_CHOICES = (
        (ERROR_STAGE_RETRIEVAL, "Retrieval"),
        (ERROR_STAGE_LLM, "LLM"),
    )

    # --- Identity ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rag_query_logs",
    )

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="rag_query_logs",
    )

    rag_space = models.ForeignKey(
        "rag_core.RagSpace",
        on_delete=models.CASCADE,
        related_name="query_logs",
    )

    # --- Core audit data ---
    question = models.TextField()

    # Réponse finale renvoyée à l'utilisateur
    # (y compris "Je ne sais pas.")
    answer = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
    )

    # Indique à quelle étape l'erreur est survenue (si status=error)
    error_stage = models.CharField(
        max_length=16,
        choices=ERROR_STAGE_CHOICES,
        blank=True,
        default="",
    )

    # Liste structurée des sources (contrat public)
    sources = models.JSONField(default=list, blank=True)

    # Version du contrat sources (permet évolution sans ambiguïté)
    sources_schema_version = models.PositiveSmallIntegerField(default=1)

    # --- Timing & trace ---
    created_at = models.DateTimeField(auto_now_add=True)
    latency_ms = models.IntegerField(null=True, blank=True)

    # Hash du prompt final envoyé au LLM (audit / reproductibilité)
    prompt_hash = models.CharField(max_length=64, blank=True, default="")

    llm_provider = models.CharField(max_length=32, blank=True, default="")
    llm_model = models.CharField(max_length=128, blank=True, default="")
    llm_prompt_tokens = models.IntegerField(null=True, blank=True)
    llm_completion_tokens = models.IntegerField(null=True, blank=True)
    llm_total_tokens = models.IntegerField(null=True, blank=True)

    # Coût LLM estimé en USD (périmètre C+). NULL = indéterminé (provider/modèle
    # non tarifé, ou décomposition prompt/completion indisponible) — jamais inventé.
    # Provider local (Ollama) = 0.0. Calculé via apps.rag_core.services.pricing.
    llm_cost_estimated = models.FloatField(null=True, blank=True)
    
    request_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="X-Request-ID propagated from HTTP layer",
    )

    policy = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="RAG decision policy (ok, no_answer, min_score, etc.)",
    )

    feedback_score = models.SmallIntegerField(
        null=True, blank=True,
        help_text="1 = positive, -1 = negative"
    )

    feedback_reason = models.CharField(
        max_length=64, blank=True, default="",
        help_text="missing_sources | incorrect | incomplete | irrelevant | other"
    )

    feedback_comment = models.TextField(blank=True, default="")

    # --- Observabilité rerank (Priorité B4) ---
    # Trace du rerank pour cette requête : {requested_provider, effective_provider,
    # fallback_used, model, n_in, n_out, latency_ms, error}. Vide ({}) si le rerank
    # n'a pas été déclenché. Le champ `fallback_used` détecte la dégradation
    # silencieuse cross_encoder -> stub (ex. CUDA indisponible en prod).
    rerank = models.JSONField(default=dict, blank=True)

    # --- Observabilité du retrieval (P1-A0) ---
    # Résumé COMPACT et NON SENSIBLE de la trace de retrieval :
    # {schema_version, entrypoint, outcome, degraded, legs:{nom: statut}}. Vide ({})
    # pour les lignes antérieures ou les appels sans user. Distingue en audit un
    # `no_answer` LÉGITIME (`degraded=false`) d'une DÉGRADATION interne
    # (`degraded=true`). Ne contient JAMAIS la question, le prompt, un chunk ni un
    # détail SQL. Voir apps.rag_core.services.query.trace.
    retrieval = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "rag_query_log"
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["workspace", "rag_space", "created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"RagQueryLog(status={self.status}, created_at={self.created_at.isoformat()})"


class LlmSettingsChangeLog(models.Model):
    """Trace d'un changement de réglages LLM (chat ET embeddings) sur un RagSpace.

    Étape 1 — Chat/Embeddings configurables par RagSpace.

    Garanties :
    - on n'écrit JAMAIS de secret (uniquement noms de provider/modèle) ;
    - une entrée = un changement effectif (chat et/ou embeddings modifié).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # SET_NULL : on conserve la trace même si l'utilisateur est supprimé.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="llm_settings_changes",
    )

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="llm_settings_changes",
    )

    rag_space = models.ForeignKey(
        "rag_core.RagSpace",
        on_delete=models.CASCADE,
        related_name="llm_settings_changes",
    )

    # Chat / génération
    old_provider = models.CharField(max_length=32, blank=True, default="")
    old_model = models.CharField(max_length=128, blank=True, default="")
    new_provider = models.CharField(max_length=32, blank=True, default="")
    new_model = models.CharField(max_length=128, blank=True, default="")

    # Embeddings / indexation
    old_embedding_provider = models.CharField(max_length=32, blank=True, default="")
    old_embedding_model = models.CharField(max_length=128, blank=True, default="")
    new_embedding_provider = models.CharField(max_length=32, blank=True, default="")
    new_embedding_model = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "llm_settings_change_log"
        indexes = [
            models.Index(fields=["workspace", "rag_space", "created_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"LlmSettingsChangeLog(rag_space={self.rag_space_id}, "
            f"{self.old_provider}/{self.old_model} -> {self.new_provider}/{self.new_model})"
        )


class ActionAuditEvent(models.Model):
    """Audit persistant **append-only** des actions (lot 5).

    Complète RagQueryLog (requêtes RAG) et CoreConfigAuditEvent (config) : trace
    les actions documentaires (upload/delete/reindex + ingestion worker) de façon
    uniforme et corrélable (request_id/task_id/resource_id).

    Garanties de confidentialité (imposées par ``apps.audit.action_audit``) :
    aucun prompt, réponse, contenu documentaire, secret, token, URL signée ou
    chemin disque n'est stocké — ``metadata`` est sanitisée et bornée.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    event_name = models.CharField(max_length=100)   # ex: action.completed
    action_name = models.CharField(max_length=100)  # ex: document.delete
    action_version = models.PositiveIntegerField(default=1)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    workspace_id = models.CharField(max_length=64, blank=True, default="")
    rag_space_id = models.UUIDField(null=True, blank=True)
    resource_type = models.CharField(max_length=50, blank=True, default="")
    resource_id = models.UUIDField(null=True, blank=True)

    request_id = models.CharField(max_length=100, blank=True, default="")
    task_id = models.CharField(max_length=255, blank=True, default="")
    idempotency_key = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(max_length=30)          # detected|started|queued|completed|failed|denied
    error_code = models.CharField(max_length=100, blank=True, default="")
    detection_method = models.CharField(max_length=50, blank=True, default="")

    metadata = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["action_name", "-created_at"]),
            models.Index(fields=["resource_id"]),
            models.Index(fields=["request_id"]),
            models.Index(fields=["task_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_name}:{self.action_name}@{self.created_at:%Y-%m-%d %H:%M:%S}"

    # --- Append-only : ni modification, ni delete unitaire -----------------
    # NB : la purge de rétention utilise ``QuerySet.delete()`` (qui court-circuite
    # cette méthode), ce qui est intentionnel — seuls les delete *unitaires*
    # (admin/code) sont bloqués.
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("ActionAuditEvent is append-only (updates are not allowed)")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("ActionAuditEvent is append-only (unit deletes are not allowed)")


class LlmEgressAuditEvent(models.Model):
    """Audit **append-only** de tout appel LLM susceptible de sortir du périmètre privé.

    Comble l'angle mort du rapport P0-C : cinq points d'appel sortants (résumé,
    réécriture de requête, mémoire de fil, intentions news) ne produisaient
    AUCUNE trace — ni provider, ni tokens, ni coût, ni événement. Un contenu
    documentaire pouvait donc partir chez un tiers sans laisser d'enregistrement.

    Cet audit couvre **tous** les call-sites, y compris les tâches Celery et le
    streaming, et distingue explicitement le provider *demandé* du provider
    *réellement utilisé* — la seule façon de détecter un repli a posteriori.

    Garanties de confidentialité (invariant testé) : ce modèle ne stocke NI
    prompt, NI chunk, NI réponse, NI secret, NI clé d'API. Seules des
    métadonnées et des mesures agrégées sont conservées (``prompt_chars`` est une
    longueur, pas un contenu).
    """

    # --- Statuts ---
    STATUS_OK = "ok"
    STATUS_DENIED = "denied"          # refus de politique (egress interdit)
    STATUS_ERROR = "error"            # échec technique

    STATUS_CHOICES = (
        (STATUS_OK, "OK"),
        (STATUS_DENIED, "Denied by policy"),
        (STATUS_ERROR, "Error"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Périmètre ---
    operation = models.CharField(max_length=64)
    workspace_id = models.CharField(max_length=64, blank=True, default="")
    rag_space_id = models.UUIDField(null=True, blank=True)
    user_id = models.CharField(max_length=64, blank=True, default="")
    correlation_id = models.CharField(max_length=64, blank=True, default="")

    # --- Décision de politique ---
    external_egress_allowed = models.BooleanField(default=False)
    policy_source = models.CharField(max_length=32, blank=True, default="")

    # --- Routage : demandé vs réellement utilisé ---
    requested_provider = models.CharField(max_length=32, blank=True, default="")
    effective_provider = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")

    gateway_attempted = models.BooleanField(default=False)
    fallback_attempted = models.BooleanField(default=False)
    fallback_used = models.BooleanField(default=False)
    #: Type d'exception ayant motivé le repli (jamais le message brut du tiers).
    fallback_reason = models.CharField(max_length=100, blank=True, default="")

    # --- Résultat ---
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OK)
    #: Nom de classe de l'erreur, sans message : un message peut contenir un écho
    #: du prompt renvoyé par le tiers (constat P0-C).
    error_type = models.CharField(max_length=100, blank=True, default="")
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    # --- Mesures (jamais de contenu) ---
    prompt_chars = models.PositiveIntegerField(null=True, blank=True)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    total_tokens = models.PositiveIntegerField(null=True, blank=True)
    cost_estimated = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "llm_egress_audit_event"
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["operation", "-created_at"]),
            models.Index(fields=["rag_space_id", "-created_at"]),
            models.Index(fields=["fallback_used", "-created_at"]),
            models.Index(fields=["correlation_id"]),
        ]

    def __str__(self) -> str:
        return (
            f"LlmEgressAuditEvent({self.operation}: {self.requested_provider}"
            f"->{self.effective_provider}, fallback={self.fallback_used})"
        )

    # --- Append-only ---
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("LlmEgressAuditEvent is append-only (updates are not allowed)")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("LlmEgressAuditEvent is append-only (unit deletes are not allowed)")


class EgressPolicyChangeLog(models.Model):
    """Audit **append-only** des changements de politique d'egress externe.

    Comble le second angle mort du rapport P0-C : basculer
    ``RagSpace.allow_external_fallback`` de False à True — le geste qui AUTORISE
    la sortie des données d'un espace — ne produisait aucun événement, nulle
    part. Le journal ``LlmSettingsChangeLog`` ne se déclenchait que sur un
    changement de provider ou de modèle.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    scope_type = models.CharField(max_length=16, default="ragspace")
    scope_ref = models.CharField(max_length=64, blank=True, default="")
    workspace_id = models.CharField(max_length=64, blank=True, default="")

    field_name = models.CharField(max_length=64)
    old_value = models.CharField(max_length=255, blank=True, default="")
    new_value = models.CharField(max_length=255, blank=True, default="")

    reason = models.CharField(max_length=255, blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "egress_policy_change_log"
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["scope_type", "scope_ref", "-created_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"EgressPolicyChangeLog({self.scope_type}:{self.scope_ref} "
            f"{self.field_name}: {self.old_value!r} -> {self.new_value!r})"
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("EgressPolicyChangeLog is append-only (updates are not allowed)")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("EgressPolicyChangeLog is append-only (unit deletes are not allowed)")
