"""Modèles du moteur tabulaire — catalogue persisté et trace d'exécution.

Ce qui est stocké, et ce qui ne l'est pas.

**Stocké** : la structure (tables, colonnes, types, unités) et la trace des exécutions
(plan, provenance, hashes). Ce sont des métadonnées : elles servent l'inventaire,
l'invalidation et l'auditabilité.

**Non stocké** : les lignes du tableau. Les recopier en base créerait une seconde
source de vérité qui divergerait du fichier au premier remplacement — exactement le
défaut que `content_sha256` sert à détecter. Les données sont relues depuis le document
source à chaque requête.

L'invalidation tient à un champ : `content_sha256`. Si le fichier change, le hash
change, et le catalogue existant ne correspond plus — il est reconstruit, jamais
réutilisé « au cas où ».
"""
from __future__ import annotations

import uuid

from django.db import models


class TabularDocumentProfile(models.Model):
    """Profil tabulaire d'un document, lié au hash de son contenu."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        "rag_core.Document", on_delete=models.CASCADE, related_name="tabular_profiles")
    rag_space = models.ForeignKey(
        "rag_core.RagSpace", on_delete=models.CASCADE, related_name="tabular_profiles")

    #: Hash du document au moment de la construction. Clé de l'invalidation.
    content_sha256 = models.CharField(max_length=64, db_index=True)
    #: Version du schéma de catalogue — permet une reconstruction ciblée.
    schema_version = models.CharField(max_length=16, default="1.0")

    STATUS_READY = "ready"
    STATUS_STALE = "stale"
    STATUS_FAILED = "failed"
    status = models.CharField(max_length=16, default=STATUS_READY, db_index=True)
    #: Cause d'un échec ou d'une dégradation — jamais un silence.
    detail = models.TextField(blank=True, default="")

    table_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["rag_space", "status"]),
            models.Index(fields=["document", "content_sha256"]),
        ]
        constraints = [
            # Un seul catalogue par (document, hash) : deux profils pour le même
            # contenu seraient deux vérités concurrentes.
            models.UniqueConstraint(
                fields=["document", "content_sha256"],
                name="uniq_tabular_profile_per_document_hash",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - confort d'admin
        return f"TabularProfile({self.document_id}, {self.content_sha256[:12]})"


class TabularTable(models.Model):
    """Une table détectée dans une feuille."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        TabularDocumentProfile, on_delete=models.CASCADE, related_name="tables")

    sheet_id = models.UUIDField()
    sheet_name = models.CharField(max_length=255)
    #: Plages Excel — la provenance en dépend, elles ne sont jamais approximées.
    data_range = models.CharField(max_length=64)
    header_range = models.CharField(max_length=64)

    row_count = models.IntegerField(default=0)
    column_count = models.IntegerField(default=0)

    #: État des formules AU NIVEAU DE LA TABLE, pas de la feuille : une table sans
    #: formule dans une feuille qui en contient ne doit pas être marquée à tort.
    has_formulas = models.BooleanField(default=False)
    has_uncached_formula = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["profile", "sheet_name"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.sheet_name}!{self.data_range}"


class TabularColumn(models.Model):
    """Une colonne d'une table, avec son type inféré et son unité."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    table = models.ForeignKey(
        TabularTable, on_delete=models.CASCADE, related_name="columns")

    original_name = models.CharField(max_length=512)
    normalized_name = models.CharField(max_length=512, db_index=True)
    position = models.IntegerField()
    inferred_type = models.CharField(max_length=32)
    #: Devise ISO ou unité. Conservée : un montant sans unité n'a pas de sens métier.
    unit = models.CharField(max_length=16, blank=True, default="")
    null_ratio = models.FloatField(default=0.0)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["table", "position"], name="uniq_tabular_column_position"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.original_name} ({self.inferred_type})"


class TabularQueryExecution(models.Model):
    """Trace d'une exécution : plan, provenance, hashes, durée.

    Append-only par usage. Le plan et la preuve sont stockés tels qu'exécutés, ce qui
    permet de rejouer une réponse et de vérifier qu'elle n'a pas dérivé.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        "rag_core.Document", on_delete=models.CASCADE, related_name="tabular_executions")

    plan_hash = models.CharField(max_length=64, db_index=True)
    evidence_hash = models.CharField(max_length=64, db_index=True)
    operation = models.CharField(max_length=32)
    schema_version = models.CharField(max_length=16, default="1.0")

    selected_row_count = models.IntegerField(default=0)
    duration_ms = models.IntegerField(default=0)

    plan = models.JSONField(default=dict)
    evidence = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["document", "created_at"]),
            models.Index(fields=["plan_hash", "created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.operation} {self.plan_hash[:12]}"
