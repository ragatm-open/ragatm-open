import uuid

from django.db import models

from apps.workspaces.models import Workspace

from .corpus import DomainCorpus


class WorkspaceDomainCorpusAccess(models.Model):
    """Association explicite workspace ↔ corpus métier (lecture/retrieval seule).

    C'est l'UNIQUE mécanisme d'autorisation : un workspace ne peut interroger un
    corpus que s'il existe une ligne ``enabled=True`` ici. Créée/supprimée
    exclusivement par le propriétaire global ragATM. La suppression d'un workspace
    ne supprime QUE ses associations (CASCADE), jamais le corpus partagé.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="domain_corpus_accesses",
    )
    corpus = models.ForeignKey(
        DomainCorpus,
        on_delete=models.CASCADE,
        related_name="workspace_accesses",
    )

    enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspace_domain_corpus_access"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "corpus"],
                name="uniq_workspace_domain_corpus",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "enabled"]),
            models.Index(fields=["corpus", "enabled"]),
        ]

    def __str__(self) -> str:
        return f"ws={self.workspace_id} <-> corpus={self.corpus_id} (enabled={self.enabled})"
