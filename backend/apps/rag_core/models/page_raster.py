"""Rasters de page et crops (CV-3C) : ``Page -> PageRaster -> Crop``.

- ``PageRaster`` : une VARIANTE de rendu d'une ``Page`` selon le contrat
  ``rasterizer_id`` + paramètres canoniques. Identité de reproduction = ``pixels_sha256``
  (modèle ID-B de CV-3A) ; ``page_raster_sha256`` n'existe que si un PNG de page est
  réellement persisté (jamais par défaut). Les versions de bibliothèques sont des
  DIAGNOSTICS, hors identité.
- ``Crop`` : sous-rectangle EXACT des pixels d'un ``PageRaster`` (contrat
  ``ragatm-crop/2``). ``crop_pixels_sha256`` = identité de reproduction ;
  ``crop_sha256`` = SHA-256 des octets PNG persistés (ceux qui seront envoyés à la
  Gateway, CV-3D). Une région > 2 MP n'est jamais redimensionnée : elle reste une ligne
  ``Crop`` SANS artefact (parent) et est découpée en tuiles encodées de côté ≤ 1176 px.

Les deux entités sont en ajout seul (cf. ``document_version``). Les règles de rendu, de
découpe et d'encodage vivent dans ``services/raster`` ; ce module ne porte que les
invariants vérifiables ligne par ligne.
"""
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.lookups import LessThanOrEqual
from django.utils import timezone

from apps.rag_core.models.document_version import Page, _AppendOnlyModel, validate_sha256_hex

#: Pixels maximum d'un crop ENVOYABLE (contrat Gateway, CV-3).
MAX_CROP_PIXELS = 2_000_000


def _optional_sha256(value) -> None:
    if value is not None:
        validate_sha256_hex(value)


class PageRaster(_AppendOnlyModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Index couvert par la contrainte unique (page, rasterizer_id, params_sha256).
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="rasters", db_index=False)
    rasterizer_id = models.CharField(max_length=64)
    rasterizer_params = models.JSONField()
    params_sha256 = models.CharField(max_length=64, validators=[validate_sha256_hex])
    width = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    height = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    pixels_sha256 = models.CharField(max_length=64, validators=[validate_sha256_hex])
    # Artefact PNG de PAGE : absent par défaut (contrat CV-3, raster non persisté).
    encoding_id = models.CharField(max_length=64, null=True, blank=True)
    page_raster_sha256 = models.CharField(max_length=64, null=True, blank=True,
                                          validators=[_optional_sha256])
    #: Versions des bibliothèques au rendu — diagnostic, jamais identité.
    implementation = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "rag_page_raster"
        constraints = [
            models.UniqueConstraint(fields=["page", "rasterizer_id", "params_sha256"],
                                    name="uniq_rag_page_raster_variant"),
            models.CheckConstraint(condition=Q(width__gte=1) & Q(height__gte=1),
                                   name="rag_page_raster_size_gte_1"),
            models.CheckConstraint(condition=Q(pixels_sha256__regex=r"^[0-9a-f]{64}$")
                                   & Q(params_sha256__regex=r"^[0-9a-f]{64}$"),
                                   name="rag_page_raster_hashes_hex"),
            models.CheckConstraint(
                condition=(Q(encoding_id__isnull=True) & Q(page_raster_sha256__isnull=True))
                | (Q(encoding_id__isnull=False) & Q(page_raster_sha256__regex=r"^[0-9a-f]{64}$")),
                name="rag_page_raster_artifact_all_or_none"),
        ]
        indexes = [models.Index(fields=["pixels_sha256"], name="rag_page_raster_pixels_idx")]

    def __str__(self) -> str:
        return f"{self.page_id}@{self.rasterizer_id}:{self.pixels_sha256[:12]}"


class Crop(_AppendOnlyModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page_raster = models.ForeignKey(PageRaster, on_delete=models.CASCADE, related_name="crops",
                                    db_index=False)
    #: Tuiles : parent = région > 2 MP. bbox des tuiles TOUJOURS relative au raster.
    parent_crop = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True,
                                    related_name="tiles")
    contract_version = models.CharField(max_length=32)
    # bbox AUTORITAIRE (= inference_bbox) en pixels du raster, [x_min, x_max) × [y_min, y_max).
    x_min = models.PositiveIntegerField()
    y_min = models.PositiveIntegerField()
    x_max = models.PositiveIntegerField()
    y_max = models.PositiveIntegerField()
    #: bbox du détecteur avant padding (même repère), ou None.
    detected_bbox = models.JSONField(null=True, blank=True)
    padding_px = models.PositiveIntegerField(default=0)
    #: {detector_id, detector_fingerprint, reason, score?} — pourquoi ce crop existe.
    origin = models.JSONField()
    #: ``origin["reason"]`` matérialisé pour l'unicité (jamais un JSON entier en contrainte).
    origin_reason = models.CharField(max_length=64)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    crop_pixels_sha256 = models.CharField(max_length=64, validators=[validate_sha256_hex])
    # Artefact PNG : présent pour une unité envoyable (≤ 2 MP), absent pour un parent.
    crop_sha256 = models.CharField(max_length=64, null=True, blank=True, validators=[_optional_sha256])
    encoding_id = models.CharField(max_length=64, null=True, blank=True)
    storage_key = models.CharField(max_length=1024, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "rag_crop"
        constraints = [
            models.UniqueConstraint(
                fields=["page_raster", "x_min", "y_min", "x_max", "y_max", "contract_version",
                        "origin_reason"],
                name="uniq_rag_crop_identity"),
            models.CheckConstraint(condition=Q(x_max__gt=F("x_min")) & Q(y_max__gt=F("y_min")),
                                   name="rag_crop_bbox_non_empty"),
            models.CheckConstraint(condition=Q(width=F("x_max") - F("x_min"))
                                   & Q(height=F("y_max") - F("y_min")),
                                   name="rag_crop_size_matches_bbox"),
            models.CheckConstraint(condition=Q(crop_pixels_sha256__regex=r"^[0-9a-f]{64}$"),
                                   name="rag_crop_pixels_sha256_hex"),
            models.CheckConstraint(
                condition=(Q(crop_sha256__isnull=True) & Q(encoding_id__isnull=True)
                           & Q(storage_key__isnull=True))
                | (Q(crop_sha256__regex=r"^[0-9a-f]{64}$") & Q(encoding_id__isnull=False)
                   & Q(storage_key__isnull=False) & ~Q(storage_key="")),
                name="rag_crop_artifact_all_or_none"),
            models.CheckConstraint(
                condition=Q(crop_sha256__isnull=True)
                | Q(LessThanOrEqual(F("width") * F("height"), MAX_CROP_PIXELS)),
                name="rag_crop_artifact_max_pixels"),
            models.CheckConstraint(condition=~Q(origin_reason=""), name="rag_crop_origin_reason_set"),
        ]
        indexes = [models.Index(fields=["crop_sha256"], name="rag_crop_sha256_idx")]

    @property
    def bbox(self) -> list:
        return [self.x_min, self.y_min, self.x_max, self.y_max]

    def clean(self):
        super().clean()
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValidationError("crop bbox must be non-empty: x_min < x_max, y_min < y_max")
        if (self.width, self.height) != (self.x_max - self.x_min, self.y_max - self.y_min):
            raise ValidationError("crop width/height must equal the bbox size")
        if self.page_raster_id and self.page_raster:
            if self.x_max > self.page_raster.width or self.y_max > self.page_raster.height:
                raise ValidationError("crop bbox exceeds the page raster")
        if self.crop_sha256 is not None and self.width * self.height > MAX_CROP_PIXELS:
            raise ValidationError("an encoded crop must not exceed 2 MP (tile it)")
        if (self.origin or {}).get("reason") != self.origin_reason:
            raise ValidationError("origin_reason must equal origin['reason']")

    def __str__(self) -> str:
        return f"{self.page_raster_id}{self.bbox}:{self.crop_pixels_sha256[:12]}"
