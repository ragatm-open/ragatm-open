"""Console de triage des incidents.

Choix structurant : l'admin affiche des **hash et des métadonnées**, jamais la
question, la réponse, le commentaire libre ni le moindre extrait documentaire.
Un outil de triage n'a pas besoin de lire le contenu pour prioriser — et une
console qui l'affiche par défaut fait de chaque personne ayant l'accès admin un
lecteur de toutes les conversations.

Le contenu reste accessible, mais délibérément derrière une action explicite et
auditée (lot 3), pas au détour d'une liste.
"""
from __future__ import annotations

from django.contrib import admin, messages

from apps.feedback.models import AnswerFeedback, FeedbackDiagnosticSnapshot
from apps.feedback.services.transitions import InvalidTransition, transition


@admin.register(AnswerFeedback)
class AnswerFeedbackAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    ordering = ("-priority_score", "-created_at")

    list_display = (
        "created_at",
        "kind",
        "status",
        "priority_score",
        "rag_space_id_hist",
        "llm_model_hist",
        "policy_hist",
        "short_question_hash",
        "has_snapshot_display",
        "expected_document",
        "expected_term",
    )
    list_filter = (
        "status",
        "kind",
        "rating",
        "priority_score",
        "llm_model_hist",
        "rag_space_id_hist",
        "created_at",
    )
    search_fields = ("dedup_fingerprint", "question_hash", "expected_term", "expected_document")

    # Tout est en lecture seule : les modifications passent par les actions
    # auditées, jamais par l'édition libre d'un formulaire.
    readonly_fields = (
        "id",
        "rating",
        "kind",
        "status",
        "priority_score",
        "dedup_fingerprint",
        "query_log_id_hist",
        "rag_space_id_hist",
        "workspace_id_hist",
        "user_pseudonym",
        "question_hash",
        "answer_hash",
        "prompt_hash",
        "sources_fingerprint",
        "policy_hist",
        "llm_provider_hist",
        "llm_model_hist",
        "log_created_at",
        "app_git_sha",
        "expected_document",
        "expected_term",
        "created_at",
        "updated_at",
        "has_snapshot_display",
    )
    exclude = ("comment", "created_by", "query_log", "message_id", "thread_id")

    actions = (
        "action_qualify",
        "action_approve_export_level_1",
        "action_mark_fixed",
        "action_close",
        "action_reject",
    )

    @admin.display(description="question (hash)")
    def short_question_hash(self, obj) -> str:
        return (obj.question_hash or "")[:12]

    @admin.display(boolean=True, description="snapshot")
    def has_snapshot_display(self, obj) -> bool:
        return obj.has_snapshot

    def has_add_permission(self, request) -> bool:
        # Un feedback naît d'un geste utilisateur, jamais d'une saisie admin.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # La suppression d'un signal se fait par la purge de rétention, pas à la main.
        return False

    # -- Actions auditées ---------------------------------------------------
    def _apply(self, request, queryset, target: str, label: str) -> None:
        done, refused = 0, 0
        for feedback in queryset:
            try:
                transition(feedback, target=target, actor=request.user, request=request)
                done += 1
            except InvalidTransition:
                refused += 1
        if done:
            self.message_user(request, f"{done} incident(s) → {label}.", messages.SUCCESS)
        if refused:
            self.message_user(
                request,
                f"{refused} incident(s) inchangé(s) : transition non autorisée depuis leur statut.",
                messages.WARNING,
            )

    @admin.action(description="Qualifier")
    def action_qualify(self, request, queryset):
        self._apply(request, queryset, AnswerFeedback.STATUS_QUALIFIED, "qualifié")

    @admin.action(description="Marquer corrigé")
    def action_mark_fixed(self, request, queryset):
        self._apply(request, queryset, AnswerFeedback.STATUS_FIXED, "corrigé")

    @admin.action(description="Classer")
    def action_close(self, request, queryset):
        self._apply(request, queryset, AnswerFeedback.STATUS_CLOSED, "classé")

    @admin.action(description="Rejeter")
    def action_reject(self, request, queryset):
        self._apply(request, queryset, AnswerFeedback.STATUS_REJECTED, "rejeté")

    @admin.action(description="Autoriser l'export — niveau 1 (métadonnées, sans texte)")
    def action_approve_export_level_1(self, request, queryset):
        """Autorise le SEUL niveau qui ne fait sortir aucun contenu.

        Les niveaux 2 et 3 exigent une justification écrite, qu'une action de
        masse ne peut pas recueillir : ils passent par
        ``manage.py export_rag_incident --justification …``. Ce n'est pas une
        limitation de l'interface, c'est le refus d'autoriser d'un clic la
        sortie d'un contenu métier.
        """
        from apps.feedback.export.levels import ExportRefused
        from apps.feedback.services.export_service import approve_export

        done, refused = 0, 0
        for feedback in queryset:
            try:
                approve_export(feedback, level_value=1, user=request.user, request=request)
                done += 1
            except (ExportRefused, Exception):  # noqa: BLE001
                refused += 1
        if done:
            self.message_user(
                request, f"{done} incident(s) autorisé(s) à l'export de niveau 1.", messages.SUCCESS
            )
        if refused:
            self.message_user(
                request,
                f"{refused} incident(s) refusé(s) : incident non qualifié, ou droits insuffisants.",
                messages.WARNING,
            )


@admin.register(FeedbackDiagnosticSnapshot)
class FeedbackDiagnosticSnapshotAdmin(admin.ModelAdmin):
    """Snapshots — consultation des MÉTADONNÉES de capture, pas du contenu.

    Le JSON du snapshot n'est volontairement pas rendu dans la liste : il porte
    des extraits documentaires. Ce que l'admin doit voir ici, c'est *quelle
    confiance accorder* à la capture (mode, troncature, échéance), pas ce
    qu'elle contient.
    """

    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_display = (
        "created_at",
        "capture_mode",
        "is_exact_capture_display",
        "contains_production_content",
        "truncated",
        "truncation_reason",
        "expires_at",
    )
    list_filter = ("capture_mode", "contains_production_content", "truncated", "expires_at")
    readonly_fields = (
        "id",
        "feedback",
        "schema_version",
        "retrieval_trace_version",
        "sources_schema_version",
        "capture_mode",
        "contains_production_content",
        "truncated",
        "truncation_reason",
        "expires_at",
        "created_at",
    )
    exclude = (
        "request_payload",
        "effective_config",
        "retrieval_trace",
        "selected_chunks",
        "generation_metadata",
    )

    @admin.display(boolean=True, description="capture exacte")
    def is_exact_capture_display(self, obj) -> bool:
        return obj.is_exact_capture

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # La purge passe par la rétention, jamais par une suppression manuelle.
        return False
