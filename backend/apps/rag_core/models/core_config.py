"""Overlay de configuration RagATM_Core stocké en base (PR-D).

Table d'OVERRIDES par portée (global / environment / workspace / ragspace). En PR-D
elle est en **lecture seule** : aucun chemin d'écriture n'est exposé (l'édition
viendra dans une PR ultérieure, gardée). Tant qu'elle est vide, le résolveur retombe
intégralement sur les settings/env => comportement strictement inchangé.

IMPORTANT : cet overlay alimente le DIAGNOSTIC (config effective affichée). Le runtime
(chat / router) continue de lire les settings ; le branchement runtime de l'overlay
est un travail ultérieur et gardé. `scope_ref` est une CharField libre (pas de FK) :
"" pour global, nom d'environnement, id workspace (str) ou uuid ragspace (str).
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class CoreConfigScopeType(models.TextChoices):
    GLOBAL = "global", "Global"
    ENVIRONMENT = "environment", "Environment"
    WORKSPACE = "workspace", "Workspace"
    RAGSPACE = "ragspace", "RagSpace"


class CoreConfigOverride(models.Model):
    """Un override de configuration Core pour une (portée, clé)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    scope_type = models.CharField(max_length=16, choices=CoreConfigScopeType.choices)
    # "" pour global ; sinon environnement, id workspace, ou uuid ragspace (en str).
    scope_ref = models.CharField(max_length=64, blank=True, default="")

    # Clé de configuration (validée contre le registre au moment de l'écriture, PR
    # ultérieure). Stockée en texte ; coercée par type côté résolveur.
    key = models.CharField(max_length=64)
    value = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="core_config_overrides",
    )

    class Meta:
        db_table = "core_config_override"
        constraints = [
            models.UniqueConstraint(
                fields=["scope_type", "scope_ref", "key"],
                name="uniq_core_config_override_scope_key",
            )
        ]
        indexes = [
            models.Index(fields=["scope_type", "scope_ref"]),
            models.Index(fields=["key"]),
        ]

    def __str__(self) -> str:
        ref = self.scope_ref or "-"
        return f"{self.scope_type}:{ref}:{self.key}"


class CoreConfigAuditAction(models.TextChoices):
    SET = "set", "Set"
    UNSET = "unset", "Unset"
    ROLLBACK = "rollback", "Rollback"
    TEST_GATEWAY = "test_gateway", "Test gateway"


class CoreConfigAuditEvent(models.Model):
    """Journal APPEND-ONLY des changements/actions de configuration Core (PR-E).

    Conçu pour PR-F (édition) et au-delà : chaque action (set/unset/rollback/
    test_gateway) crée un événement immuable. En PR-E, AUCUN chemin de production
    ne l'écrit (pas d'édition exposée) ; seul le helper serveur ``record_config_event``
    peut créer une entrée. Les mises à jour et suppressions d'instance sont
    interdites (garde-fou append-only).

    On ne stocke JAMAIS de secret. Les valeurs de clés sensibles (ex.
    AI_GATEWAY_BASE_URL) sont redactées à la LECTURE via l'API, pas ici.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    action = models.CharField(max_length=16, choices=CoreConfigAuditAction.choices)

    # Portée + clé concernées (vides possibles, ex. test_gateway).
    scope_type = models.CharField(max_length=16, blank=True, default="")
    scope_ref = models.CharField(max_length=64, blank=True, default="")
    key = models.CharField(max_length=64, blank=True, default="")

    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")

    environment = models.CharField(max_length=32, blank=True, default="")
    reason = models.TextField(blank=True, default="")

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="core_config_audit_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_config_audit_event"
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["key", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action}:{self.key or '-'}@{self.created_at:%Y-%m-%d}"

    def save(self, *args, **kwargs):
        # Append-only : interdit la modification d'un événement existant.
        # NB : ON_DELETE=SET_NULL de `actor` passe par un UPDATE de queryset (pas
        # save()), donc la suppression d'un utilisateur n'est PAS bloquée ici.
        if not self._state.adding:
            raise ValueError("CoreConfigAuditEvent is append-only (updates are not allowed)")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("CoreConfigAuditEvent is append-only (deletes are not allowed)")
