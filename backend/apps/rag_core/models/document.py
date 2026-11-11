import os
import uuid

from django.db import models

from apps.rag_core.models.rag_space import RagSpace


class DocumentStatus(models.TextChoices):
    # NOTE: Keep values UPPERCASE to match DB, filters, and partial unique index logic.
    PENDING = "PENDING", "Pending"
    INGESTING = "INGESTING", "Ingesting"
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"


def _sanitize_filename(filename: str) -> str:
    """
    Defensive filename sanitation:
    - keep basename only (no directories)
    - replace path/line-break oddities
    - keep it non-empty
    """
    safe = os.path.basename(filename or "").strip() or "document.bin"
    # Avoid control chars / newlines in storage backends
    safe = safe.replace("\n", "_").replace("\r", "_").replace("\t", "_")
    return safe


def document_upload_to(instance: "Document", filename: str) -> str:
    """
    Store originals under MEDIA_ROOT/rag_docs/<rag_space_id>/<document_id>/<filename>

    - deterministic path
    - avoids collisions
    - allows easy cleanup per rag_space/doc

    NOTE: Keep storage_file.max_length large enough because this path includes:
      - rag_docs/<rag_space_uuid>/<doc_uuid>/<filename>
    """
    safe = _sanitize_filename(filename)
    return f"rag_docs/{instance.rag_space_id}/{instance.id}/{safe}"


class Document(models.Model):
    """
    Métadonnées documentaires. Le contenu indexé (chunks) vit dans Chunk.
    On conserve en option le fichier original pour permettre "download/view".
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    rag_space = models.ForeignKey(
        RagSpace, on_delete=models.CASCADE, related_name="documents"
    )

    filename = models.CharField(max_length=512)
    content_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)

    # SHA-256 hex digest of the original bytes or normalized text (depending on ingest path)
    # Blank/empty means "no dedup key available".
    sha256 = models.CharField(max_length=64, blank=True, default="")

    # ✅ Original file storage (optional for backward compatibility)
    # IMPORTANT: max_length must be large enough for deep upload_to paths + long filenames.
    storage_file = models.FileField(
        upload_to=document_upload_to,
        max_length=1024,  # ✅ FIX: avoid storage_file_save_failed on long paths/filenames
        null=True,
        blank=True,
    )

    # ✅ Date extracted from document CONTENT (not from filename).
    # Examples: press review date, report date, publication date if present in the text.
    document_date = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=DocumentStatus.choices,
        default=DocumentStatus.PENDING,
    )
    error_message = models.TextField(blank=True, default="")

    # Métadonnées d'ingestion/extraction (format détecté, méthode, OCR, warnings,
    # statut d'extraction détaillé : success/partial/empty/failed/rejected...).
    # JSON libre pour évolution sans migration. Jamais de contenu extrait ici.
    meta = models.JSONField(default=dict, blank=True)

    # --- Empreinte d'embeddings de l'indexation (traçabilité + cohérence) ---
    # Provider/modèle d'embeddings RÉELLEMENT utilisés pour indexer les chunks de
    # CE document (snapshot pris à l'ingestion depuis le RagSpace). Sert à :
    #  - détecter qu'un document est devenu incohérent après un changement de
    #    configuration d'embeddings de l'espace (reindex_required) ;
    #  - filtrer la recherche vectorielle sur les seuls documents encodés avec le
    #    modèle courant (jamais de comparaison entre vecteurs de modèles différents).
    # Vide = document ancien non encore ré-indexé (empreinte inconnue).
    embedding_provider = models.CharField(max_length=32, blank=True, default="")
    embedding_model = models.CharField(max_length=128, blank=True, default="")
    # True quand la config d'embeddings de l'espace a changé depuis l'indexation de
    # ce document : les vecteurs existants ne sont plus comparables au modèle
    # courant → document exclu du leg vectoriel jusqu'à réindexation.
    reindex_required = models.BooleanField(default=False)

    # --- RagATM_Core scoping (additif, inerte pour l'instant) ---
    # Métadonnées de scoping introduites pour la trajectoire RagATM_Core
    # (cf. rapport d'évaluation, PR 1). Inertes dans cette PR : aucun pipeline,
    # retrieval, permission ou endpoint ne les consomme encore.
    #
    # IMPORTANT : `document_status` est le statut MÉTIER du document et est
    # volontairement DISTINCT de `status` (statut TECHNIQUE d'ingestion ci-dessus,
    # PENDING/INGESTING/READY/FAILED). Les deux ne doivent jamais être confondus.
    # Les enum/choices métier (DRAFT/VALIDATED/PUBLISHED/...) sont différés à une PR
    # ultérieure ; ici on conserve une CharField libre avec défaut "active".
    collection = models.CharField(max_length=128, default="legacy")
    source_app = models.CharField(max_length=64, default="ragatm")
    confidentiality = models.CharField(max_length=32, default="internal")
    document_status = models.CharField(max_length=32, default="active")
    version = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    # Début de la tentative d'ingestion/réindexation COURANTE, posé dans le même UPDATE
    # que `status=INGESTING` (cf. services/ingest/attempts.py). Le reaper n'y lit que
    # l'ÂGE d'une tentative : une tentative plus jeune que le seuil peut être vivante.
    # NULL pour les lignes antérieures au champ (repli sur `created_at`).
    ingest_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rag_document"
        indexes = [
            models.Index(fields=["rag_space", "created_at"]),
            models.Index(fields=["rag_space", "status"]),
            # ✅ speed-up de-dup queries
            models.Index(fields=["rag_space", "sha256"]),
            # ✅ speed-up "news" queries / date filters
            models.Index(fields=["rag_space", "document_date"]),
        ]
        # ✅ HARD de-dup (anti-concurrency): one active doc per (rag_space, sha256)
        # Only enforced when sha256 is not empty AND status is not FAILED.
        constraints = [
            models.UniqueConstraint(
                fields=["rag_space", "sha256"],
                condition=(~models.Q(sha256="") & ~models.Q(status=DocumentStatus.FAILED)),
                name="uniq_rag_document_space_sha256_not_failed",
            )
        ]

    def __str__(self) -> str:
        return f"{self.filename} ({self.id})"

    @property
    def original_filename(self) -> str:
        """
        Best-effort: keep using stored filename; if empty, fallback to storage_file name.
        """
        if self.filename:
            return self.filename
        if self.storage_file and getattr(self.storage_file, "name", ""):
            return os.path.basename(self.storage_file.name)
        return str(self.id)
