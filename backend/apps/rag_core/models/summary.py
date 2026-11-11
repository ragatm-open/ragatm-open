# backend/apps/rag_core/models/summary.py
"""Résumé persisté d'un document (cache + traçabilité coûts).

Étape 4a. Un enregistrement = une variante de résumé (mode × scope × page ×
langue) pour un document. La validité est contrôlée par ``source_fingerprint``
(empreinte des chunks utilisés) : si le document est réindexé, les chunks
changent → l'empreinte change → le cache est ignoré et régénéré. La suppression
du document supprime ses résumés (CASCADE).
"""
from __future__ import annotations

import uuid

from django.db import models

from apps.rag_core.models.document import Document
from apps.rag_core.models.rag_space import RagSpace


class SummaryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"  # généré en async (document long), en cours
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"


class DocumentSummary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # rag_space dupliqué pour l'étanchéité par requête (comme Chunk).
    rag_space = models.ForeignKey(RagSpace, on_delete=models.CASCADE, related_name="document_summaries")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="summaries")

    # Clé de variante déterministe : "{mode}:{scope}:{page|''}:{language}".
    variant_key = models.CharField(max_length=80)

    mode = models.CharField(max_length=16, default="bullets")
    scope = models.CharField(max_length=16, default="document")
    page = models.IntegerField(null=True, blank=True)
    language = models.CharField(max_length=8, default="fr")

    summary = models.TextField(blank=True, default="")
    truncated = models.BooleanField(default=False)

    status = models.CharField(max_length=16, choices=SummaryStatus.choices, default=SummaryStatus.READY)
    error_message = models.TextField(blank=True, default="")

    # Empreinte des chunks ayant servi à générer ce résumé (invalidation au reindex).
    source_fingerprint = models.CharField(max_length=64, blank=True, default="")

    # Coûts / traçabilité LLM
    provider = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_tokens = models.IntegerField(null=True, blank=True)
    completion_tokens = models.IntegerField(null=True, blank=True)
    total_tokens = models.IntegerField(null=True, blank=True)
    cost_usd = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_document_summary"
        constraints = [
            models.UniqueConstraint(fields=["document", "variant_key"], name="uniq_summary_document_variant"),
        ]
        indexes = [
            models.Index(fields=["rag_space", "document"]),
        ]

    def __str__(self) -> str:
        return f"DocumentSummary({self.document_id}, {self.variant_key})"
