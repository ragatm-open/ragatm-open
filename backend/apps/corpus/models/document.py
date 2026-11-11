import os
import uuid

from django.db import models

from .corpus import DomainCorpus


class CorpusDocumentStatus(models.TextChoices):
    # Valeurs en MAJUSCULES, alignées sur rag_core.DocumentStatus (filtres, index partiel).
    PENDING = "PENDING", "Pending"
    INGESTING = "INGESTING", "Ingesting"
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"


def _sanitize_filename(filename: str) -> str:
    safe = os.path.basename(filename or "").strip() or "document.bin"
    return safe.replace("\n", "_").replace("\r", "_").replace("\t", "_")


def corpus_document_upload_to(instance: "DomainCorpusDocument", filename: str) -> str:
    """Stockage des originaux : MEDIA_ROOT/corpus_docs/<corpus_id>/<document_id>/<file>.

    Chemin séparé des documents privés (rag_docs/...) : aucune confusion possible
    entre la couche privée des workspaces et la couche corpus métier partagée.
    """
    safe = _sanitize_filename(filename)
    return f"corpus_docs/{instance.corpus_id}/{instance.id}/{safe}"


class DomainCorpusDocument(models.Model):
    """Document appartenant à un corpus métier (jamais à un workspace).

    Table strictement distincte de ``rag_document`` : une réindexation ou
    suppression de corpus ne peut JAMAIS toucher les documents privés.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    corpus = models.ForeignKey(
        DomainCorpus,
        on_delete=models.CASCADE,
        related_name="documents",
    )

    title = models.CharField(max_length=512, blank=True, default="")
    original_filename = models.CharField(max_length=512)
    mime_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")

    storage_file = models.FileField(
        upload_to=corpus_document_upload_to,
        max_length=1024,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=16,
        choices=CorpusDocumentStatus.choices,
        default=CorpusDocumentStatus.PENDING,
    )
    error_message = models.TextField(blank=True, default="")

    # Provenance documentaire (référence réglementaire, URL source, version).
    source_url = models.URLField(max_length=1024, blank=True, default="")
    source_reference = models.CharField(max_length=512, blank=True, default="")
    version_label = models.CharField(max_length=64, blank=True, default="")

    # --- Empreinte d'INDEXATION (identité des vecteurs réellement stockés) ---
    #
    # Pourquoi au niveau DOCUMENT et non du chunk. `index_corpus_extraction_result`
    # indexe un document en UNE transaction : suppression des anciens chunks, un seul
    # appel d'embeddings pour tous les specs, création en lot, statut READY. Un échec
    # supprime les chunks et pose FAILED. Un document ne peut donc jamais porter deux
    # modèles ; une empreinte par chunk serait redondante et 1341 fois plus coûteuse à
    # vérifier.
    #
    # Ces champs décrivent CE QUI A PRODUIT LES VECTEURS, jamais ce que le corpus
    # déclare aujourd'hui. C'est toute leur utilité : leur divergence avec
    # `DomainCorpus.embedding_*` est exactement le signal qu'une comparaison croisée
    # deviendrait possible.
    #
    # Vide = provenance NON PROUVÉE (documents antérieurs à ce lot). Jamais renseigné
    # par supposition : seule la commande `backfill_corpus_embedding_fingerprints`
    # écrit, et seulement sur preuve.
    embedding_provider = models.CharField(max_length=32, blank=True, default="")
    embedding_model = models.CharField(max_length=128, blank=True, default="")
    embedding_dimension = models.PositiveIntegerField(null=True, blank=True)
    #: sha256(version_pipeline|provider|modèle|dimension) — cf. `EmbeddingIdentity`.
    #: Un seul champ indexé suffit alors à comparer deux identités, et une évolution du
    #: pipeline invisible dans le couple (provider, modèle) reste détectable.
    embedding_fingerprint = models.CharField(max_length=64, blank=True, default="")
    #: Posé quand l'identité du corpus change : les vecteurs existants restent en place
    #: mais ne sont plus réputés comparables tant qu'ils n'ont pas été refaits.
    reindex_required = models.BooleanField(default=False)

    # Métadonnées d'ingestion/extraction (jamais le texte extrait).
    meta = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "domain_corpus_document"
        indexes = [
            models.Index(fields=["corpus", "created_at"]),
            models.Index(fields=["corpus", "status"]),
            models.Index(fields=["corpus", "sha256"]),
            # Le filtre de retrieval interroge (corpus, statut, empreinte, réindexation) :
            # l'index couvre exactement cette clause.
            models.Index(fields=["corpus", "status", "embedding_fingerprint"]),
            models.Index(fields=["corpus", "reindex_required"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["corpus", "sha256"],
                condition=(~models.Q(sha256="") & ~models.Q(status=CorpusDocumentStatus.FAILED)),
                name="uniq_corpus_document_sha256_not_failed",
            )
        ]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.id})"

    @property
    def display_title(self) -> str:
        return self.title or self.original_filename or str(self.id)
