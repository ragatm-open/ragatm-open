from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class PromptTemplate(models.Model):
    """Template de prompt versionné, partagé au niveau workspace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="prompt_templates",
    )

    name = models.CharField(max_length=128)
    version = models.PositiveIntegerField()
    content = models.TextField()

    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "prompt_template"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name", "version"],
                name="uniq_prompt_tpl_workspace_name_ver",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "name"]),
            models.Index(fields=["workspace", "name", "is_active"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"PromptTemplate({self.workspace_id}, {self.name}, v{self.version}, active={self.is_active})"


class ChatThread(models.Model):
    """
    Conversation persistante (par user + rag_space).
    Laisse thread_id optionnel côté API : si absent, création auto.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_threads",
    )

    rag_space = models.ForeignKey(
        "rag_core.RagSpace",
        on_delete=models.CASCADE,
        related_name="chat_threads",
    )

    title = models.CharField(max_length=160, null=True, blank=True)

    # ------------------------------------------------------------------
    # Mémoire hiérarchique (niveau 2): summary + fenêtre récente
    # ------------------------------------------------------------------
    memory_summary = models.TextField(null=True, blank=True)
    memory_summary_until_message_id = models.UUIDField(null=True, blank=True)
    memory_summary_updated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "chat_thread"
        indexes = [
            models.Index(fields=["user", "rag_space", "-updated_at"]),
            models.Index(fields=["rag_space", "-updated_at"]),
        ]

    def __str__(self) -> str:
        return f"ChatThread({self.id}, user={self.user_id}, rag_space={self.rag_space_id})"


class ChatMessage(models.Model):
    ROLE_SYSTEM = "system"
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"

    ROLE_CHOICES = [
        (ROLE_SYSTEM, ROLE_SYSTEM),
        (ROLE_USER, ROLE_USER),
        (ROLE_ASSISTANT, ROLE_ASSISTANT),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    thread = models.ForeignKey(
        ChatThread,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField()

    # Snapshot des sources normalisées renvoyées à l'UI (ou None)
    sources = models.JSONField(null=True, blank=True)

    # Corrélation / observabilité
    request_id = models.CharField(max_length=64, null=True, blank=True)

    # Lien logique vers RagQueryLog (UUID), sans FK dure pour éviter couplage/migrations croisées
    rag_query_log_id = models.UUIDField(null=True, blank=True)

    # Lien logique vers CorpusAnalysisJob (UUID), même convention que ci-dessus.
    # C'est ce champ qui permet de replacer la carte d'analyse SOUS SON message
    # après un rechargement du fil : sans lui, l'historique ne sait plus quel
    # message a lancé quelle analyse. Le lien job -> fil ne suffit pas, car un job
    # réutilisé (idempotence) reste rattaché au fil où il a été créé.
    analysis_id = models.UUIDField(null=True, blank=True)

    # --- Résultats persistés produits hors du flux question/réponse -----------
    # Un résumé de document demandé depuis le panneau latéral doit rester dans le
    # fil après une nouvelle question, un refresh ou une réouverture. Il est donc
    # écrit ici comme un vrai message, et non gardé dans l'état React.
    #
    # ``kind`` vide = message de chat standard : les messages antérieurs à cette
    # migration restent donc valides sans aucun backfill, et le frontend dégrade
    # vers le rendu normal quand il ne reconnaît pas la valeur.
    KIND_DOCUMENT_SUMMARY = "document_summary"

    kind = models.CharField(max_length=32, default="", blank=True)

    # ``status`` vide = sans cycle de vie (cas des messages de chat classiques,
    # qui naissent déjà terminés). Seuls les messages à génération différée
    # portent pending/completed/failed.
    STATUS_PENDING = "pending"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    status = models.CharField(max_length=16, default="", blank=True)

    # Liens LOGIQUES (UUID sans FK dure), même convention que analysis_id et
    # rag_query_log_id : le message doit rester lisible si le document ou le
    # résumé sont supprimés ensuite. Le contenu est un instantané autonome.
    document_id = models.UUIDField(null=True, blank=True)
    summary_id = models.UUIDField(null=True, blank=True)

    # Contrat de PROVENANCE des messages produits hors flux question/réponse.
    #
    # ``document_id`` est un UUID logique sans FK. Depuis l'arrivée du corpus métier
    # il peut désigner un ``Document`` privé OU un ``DomainCorpusDocument`` : deux
    # espaces de noms disjoints, indistinguables par la seule valeur. Sans
    # discriminant persisté, relire un fil ancien ne permettrait plus de savoir quel
    # document a été résumé — et rejouer l'action viserait le mauvais modèle.
    #
    # ``None`` = message antérieur à ce champ, ou message de chat ordinaire : aucun
    # backfill n'est nécessaire, les lecteurs retombent sur ``workspace_document``
    # (cf. ``summary_messages.source_ref_of``), qui est ce qu'ils étaient déjà.
    #
    # Forme attendue pour un résumé (cf. docs/03-backend/resume-corpus-metier.md) :
    #   {"message_type": "document_summary",
    #    "source": {"source_type": "...", "document_id": "...", "corpus_id": ...,
    #               "corpus_name": ..., "filename": ..., "page_number": ...},
    #    "summary": {"scope": "...", "mode": "...", "language": "..."}}
    meta = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "chat_message"
        indexes = [
            models.Index(fields=["thread", "created_at"]),
            models.Index(fields=["thread", "role"]),
            # Fan-out de finalisation : un DocumentSummary est un CACHE partagé
            # (unique sur document+variante), donc plusieurs messages — de fils et
            # d'utilisateurs différents — peuvent attendre le même résumé.
            models.Index(fields=["summary_id", "status"]),
        ]
        constraints = [
            # Idempotence RÉELLE, garantie par la base et non par un check-then-act.
            # Volontairement limitée aux messages de résumé : ces lignes n'existent
            # qu'à partir de cette migration, donc la contrainte ne peut pas entrer
            # en conflit avec des doublons historiques et le déploiement est sûr.
            # (La même garantie manque au chat standard — dette documentée à part.)
            models.UniqueConstraint(
                fields=["thread", "request_id", "role"],
                # Littéral et non KIND_DOCUMENT_SUMMARY : le corps de Meta ne voit
                # pas les attributs de la classe englobante.
                condition=models.Q(kind="document_summary") & ~models.Q(request_id=""),
                name="uniq_chat_message_summary_request",
            ),
        ]

    def __str__(self) -> str:
        return f"ChatMessage({self.id}, thread={self.thread_id}, role={self.role})"


class RephrasingSuggestion(models.Model):
    """Résultat ASYNCHRONE des suggestions de reformulation (cas no_answer, F2).

    Découple la génération LLM du worker HTTP : le chat enfile une tâche Celery et
    ferme immédiatement le flux ; le résultat est persisté ici puis récupéré par le
    frontend via un endpoint dédié. Clé d'idempotence = (thread, request_id).

    Confidentialité : ne stocke NI le prompt, NI le contexte documentaire, NI la question
    — uniquement les suggestions finales et un ``error_code`` borné.
    """

    STATUS_PENDING = "pending"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "pending"),
        (STATUS_READY, "ready"),
        (STATUS_FAILED, "failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        "chat.ChatThread", on_delete=models.CASCADE, related_name="rephrasing_suggestions"
    )
    request_id = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    suggestions = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["thread", "request_id"], name="uniq_rephrasing_thread_request"),
        ]
        indexes = [models.Index(fields=["thread", "-created_at"])]

    def __str__(self) -> str:
        return f"RephrasingSuggestion(thread={self.thread_id}, request_id={self.request_id}, status={self.status})"
