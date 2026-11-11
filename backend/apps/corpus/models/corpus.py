import uuid

from django.db import models

from .domain import BusinessDomain


class DomainCorpus(models.Model):
    """Corpus documentaire métier partagé entre plusieurs workspaces.

    Étanchéité : un corpus N'APPARTIENT À AUCUN workspace. Il est rattaché à un
    domaine métier et rendu accessible (en lecture/retrieval) aux workspaces via
    la table d'association ``WorkspaceDomainCorpusAccess``. Géré exclusivement par
    le propriétaire global ragATM.

    Provider/modèle d'embeddings PROPRES au corpus (comme RagSpace pour le privé) :
    les vecteurs d'un corpus ne sont comparables qu'avec le même provider/modèle.
    On ne mélange jamais des embeddings incompatibles : la recherche corpus encode
    la question avec le provider/modèle du corpus interrogé.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    domain = models.ForeignKey(
        BusinessDomain,
        on_delete=models.CASCADE,
        related_name="corpora",
    )

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80)
    description = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)

    # Embeddings / indexation — configurables PAR corpus. RAGatm Open : pipeline GOUVERNÉ
    # de l'AI Gateway uniquement (profil logique du registre). Changer ces champs impose
    # une réindexation du corpus.
    embedding_provider = models.CharField(
        max_length=32,
        default="ai_gateway",
        choices=[("ai_gateway", "AI Gateway (pipeline gouverné)")],
    )
    embedding_model = models.CharField(
        max_length=128,
        blank=True,
        default="legacy-768",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "domain_corpus"
        ordering = ["domain_id", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["domain", "slug"],
                name="uniq_domain_corpus_slug",
            ),
        ]
        indexes = [
            models.Index(fields=["domain"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["embedding_provider", "embedding_model"]),
        ]

    def save(self, *args, **kwargs):
        """Propage un changement d'identité d'embeddings, quelle que soit la voie.

        L'API n'est pas le seul chemin d'écriture : l'admin Django, le shell et toute
        commande future écrivent directement le modèle. Placer la garde ici — et pas
        seulement dans la vue — est ce qui rend l'invariant vrai partout : après un
        changement de provider ou de modèle, aucun document ne reste réputé cohérent.

        N'écrit que des drapeaux. Les vecteurs, les chunks et les fichiers ne sont
        jamais touchés : le rollback est un retour de valeur.
        """
        from apps.corpus.services.embedding_consistency import (
            apply_identity_change,
            identity_changed,
        )

        changed = False
        if self.pk:
            previous = DomainCorpus.objects.filter(pk=self.pk).only(
                "embedding_provider", "embedding_model"
            ).first()
            changed = previous is not None and identity_changed(
                previous, provider=self.embedding_provider, model=self.embedding_model
            )
        super().save(*args, **kwargs)
        if changed:
            apply_identity_change(self)

    def __str__(self) -> str:
        return f"{self.name} [{self.domain_id}]"
