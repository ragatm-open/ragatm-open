# backend/apps/rag_core/models/benchmark.py
from __future__ import annotations

from django.db import models


class RagBenchmarkQuestion(models.Model):
    rag_space_id = models.UUIDField(db_index=True)
    question = models.TextField()
    is_active = models.BooleanField(default=True)

    # optionnel : aide à l’éval sans “answer key” complet
    expected_keywords = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["rag_space_id", "is_active", "created_at"]),
        ]
