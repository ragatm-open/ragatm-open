"""Fondation de provenance documentaire (CV-3B) : ``DocumentVersion`` puis ``Page``.

    Document          identité LOGIQUE (inchangée, UUID existant)
      -> DocumentVersion   octets IMMUABLES d'une version du document
           -> Page         page PHYSIQUE de cette version (PDF seulement)

Les deux entités sont **en ajout seul** : une ligne écrite n'est jamais modifiée par un
chemin supporté (``save()`` sur une ligne existante, ``QuerySet.update`` /
``bulk_update``). Une nouvelle version d'octets = une NOUVELLE ``DocumentVersion`` ;
une géométrie différente pour les mêmes octets est un conflit, jamais une mise à jour.

Limite assumée : un ``UPDATE`` SQL direct (shell, ``raw``) contourne ces gardes — aucun
trigger n'est posé. La suppression reste permise : elle suit la cascade du ``Document``
(service de suppression existant), seul chemin de suppression supporté.

Hors périmètre (CV-3C et suivants) : rasters de page, crops, observations visuelles.
"""
import math
import re
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.rag_core.models.document import Document

#: Valeurs PDF admises pour ``/Rotate`` (normalisées modulo 360).
PAGE_ROTATIONS = (0, 90, 180, 270)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ImmutableRowError(ValueError):
    """Tentative de modification d'une ligne en ajout seul."""


class _AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRowError(f"{self.model.__name__} is append-only (updates are not allowed)")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutableRowError(f"{self.model.__name__} is append-only (updates are not allowed)")


class _AppendOnlyModel(models.Model):
    objects = _AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRowError(f"{type(self).__name__} is append-only (updates are not allowed)")
        self.full_clean(validate_unique=False, validate_constraints=False)
        super().save(*args, **kwargs)


def validate_pdf_box(value) -> None:
    """Boîte PDF ``[x0, y0, x1, y1]`` en points, normalisée (x1 > x0, y1 > y0).

    Nombres JSON finis uniquement (pas de booléen, pas de chaîne) : la valeur stockée
    est exactement celle lue dans le PDF, sans arrondi ni conversion raster.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValidationError("PDF box must be a list of 4 numbers [x0, y0, x1, y1]")
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValidationError("PDF box coordinates must be finite numbers")
    x0, y0, x1, y1 = value
    if not (x1 > x0 and y1 > y0):
        raise ValidationError("PDF box must satisfy x1 > x0 and y1 > y0")


def validate_sha256_hex(value) -> None:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ValidationError("content_sha256 must be 64 lowercase hex characters")


class DocumentVersion(_AppendOnlyModel):
    """Version IMMUABLE des octets d'un ``Document``.

    ``content_sha256`` reprend EXACTEMENT la définition de ``Document.sha256`` (SHA-256
    des octets bruts stockés) ; ``storage_key`` est le nom LOGIQUE du fichier dans le
    stockage Django (``storage_file.name``, relatif à ``MEDIA_ROOT``), jamais un chemin
    machine. Un ``Document`` porte N versions (aujourd'hui N = 1 : aucun flux de
    remplacement n'existe).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Index couvert par la contrainte unique (document, content_sha256) : pas de doublon.
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="versions", db_index=False
    )
    content_sha256 = models.CharField(max_length=64, validators=[validate_sha256_hex])
    size_bytes = models.BigIntegerField()
    storage_key = models.CharField(max_length=1024)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "rag_document_version"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "content_sha256"], name="uniq_rag_document_version_doc_sha"
            ),
            models.CheckConstraint(
                condition=models.Q(content_sha256__regex=r"^[0-9a-f]{64}$"),
                name="rag_document_version_sha256_hex",
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gte=0), name="rag_document_version_size_gte_0"
            ),
            models.CheckConstraint(
                condition=~models.Q(storage_key=""), name="rag_document_version_storage_key_set"
            ),
        ]
        indexes = [models.Index(fields=["content_sha256"], name="rag_docver_sha256_idx")]

    def __str__(self) -> str:
        return f"{self.document_id}@{self.content_sha256[:12]}"


class Page(_AppendOnlyModel):
    """Page PHYSIQUE d'une ``DocumentVersion`` (PDF).

    - ``page_number`` : ordre physique de l'arbre des pages, **1-based** — même
      convention que ``Chunk.page_number`` et le lien viewer ``#page=N`` ; ce n'est PAS
      le numéro imprimé.
    - ``page_label`` : libellé ``/PageLabels`` (``"xi"``, ``"A-12"``…), affichage seul,
      jamais une clé ; ``None`` si le PDF n'en déclare pas.
    - ``mediabox`` / ``cropbox`` : ``[x0, y0, x1, y1]`` en points PDF, coins normalisés,
      valeurs exactes du fichier (``cropbox`` = MediaBox quand le PDF n'en déclare pas).
    - ``rotate`` : ``/Rotate`` source (0/90/180/270), PAS une rotation de raster.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Index couvert par la contrainte unique (document_version, page_number).
    document_version = models.ForeignKey(
        DocumentVersion, on_delete=models.CASCADE, related_name="pages", db_index=False
    )
    page_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    page_label = models.CharField(max_length=255, null=True, blank=True)
    mediabox = models.JSONField(validators=[validate_pdf_box])
    cropbox = models.JSONField(validators=[validate_pdf_box])
    rotate = models.PositiveSmallIntegerField(choices=[(r, str(r)) for r in PAGE_ROTATIONS])
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "rag_document_page"
        constraints = [
            models.UniqueConstraint(
                fields=["document_version", "page_number"], name="uniq_rag_document_page_version_number"
            ),
            models.CheckConstraint(
                condition=models.Q(page_number__gte=1), name="rag_document_page_number_gte_1"
            ),
            models.CheckConstraint(
                condition=models.Q(rotate__in=PAGE_ROTATIONS), name="rag_document_page_rotate_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_version_id} p.{self.page_number}"
