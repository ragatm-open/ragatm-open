import uuid

from django.db import models


class BusinessDomain(models.Model):
    """Problématique métier (construction, finance, sécurité informatique, ...).

    Niveau de classement le plus haut de la couche documentaire métier. Géré
    EXCLUSIVEMENT par le propriétaire global ragATM. N'a aucun lien avec un
    workspace : un domaine regroupe un ou plusieurs corpus partageables.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "business_domain"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"
