# backend/apps/audit/admin.py
"""Admin LECTURE SEULE pour l'audit d'action (lot 5).

Aucune édition, aucune suppression unitaire, aucune création. Les métadonnées
sont déjà sanitisées à l'écriture ; l'affichage reste en lecture seule.
"""
from __future__ import annotations

from django.contrib import admin

from .models import ActionAuditEvent


@admin.register(ActionAuditEvent)
class ActionAuditEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "event_name", "action_name", "status",
        "resource_type", "resource_id", "request_id", "task_id",
    )
    list_filter = ("action_name", "event_name", "status")
    search_fields = ("request_id", "task_id", "resource_id", "rag_space_id")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
