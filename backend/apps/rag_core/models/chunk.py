import uuid

from django.db import models
from django.db.models import JSONField
from pgvector.django import VectorField

from apps.rag_core.models.document import Document
from apps.rag_core.models.rag_space import RagSpace


# Dimension RÉELLE de la colonne pgvector `rag_chunk.embedding`, figée par la
# migration 0001 (`VectorField(dimensions=768)`). Elle NE doit PAS suivre
# dynamiquement le setting `RAG_EMBEDDING_DIM` : la déclaration du modèle doit
# refléter la colonne migrée, sinon Django génère une migration `AlterField`
# destructrice et l'ingestion casse silencieusement.
#
# Pour changer de dimension : (1) migration `AlterField(embedding, dimensions=N)`,
# (2) réembedding complet de tous les chunks, (3) reconstruction des index
# vectoriels (HNSW/ivfflat). Le system check `rag_core.E001` (apps.py) échoue au
# démarrage si `RAG_EMBEDDING_DIM` diverge de cette valeur.
EMBEDDING_VECTOR_DIM = 768
DEFAULT_EMBEDDING_DIM = EMBEDDING_VECTOR_DIM  # rétro-compat


class Chunk(models.Model):
    """
    Unité indexée : texte + embedding + provenance.
    IMPORTANT: rag_space est dupliqué ici pour garantir l'étanchéité par requête.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    rag_space = models.ForeignKey(RagSpace, on_delete=models.CASCADE, related_name="chunks")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")

    chunk_index = models.IntegerField(default=0)

    text = models.TextField()
    embedding = VectorField(dimensions=EMBEDDING_VECTOR_DIM, null=True, blank=True)

    # --- Identité du pipeline d'embeddings (lot 3) ---------------------------
    #
    # `Document.embedding_provider`/`embedding_model` disent déjà avec QUELLE CONFIG un
    # document a été indexé. Ils ne disent pas avec quels POIDS : deux versions du même
    # modèle logique portent le même nom et produisent des vecteurs incomparables. Le
    # `pipeline_id` rendu par l'AI Gateway porte, lui, le digest des poids.
    #
    # Il est stocké VERBATIM — jamais reconstruit ni interprété localement. Trois états
    # possibles, et ils ne veulent pas dire la même chose (cf.
    # apps/llm_router/embedding_pipeline.py) :
    #
    #   `emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8` identité GOUVERNÉE, OPAQUE : validée par
    #                                           égalité avec la référence publiée par la
    #                                           Gateway (/health/embeddings), digest vérifié
    #   `legacy-768@0a109f42/document-v1`       identité GOUVERNÉE au format v1 historique :
    #                                           valide à son époque, conservée telle quelle
    #   `legacy:ollama:nomic-embed-text:768:…`  identité LEGACY, construite localement,
    #                                           sans digest, apposée aux indexations
    #                                           NOUVELLES du chemin historique
    #   NULL                                    chunk HISTORIQUE : on ne sait pas, et on
    #                                           refuse de l'étiqueter sans réindexation
    #
    # La colonne est nullable et le RESTE : rétro-étiqueter les chunks existants
    # inventerait une provenance invérifiable — précisément le mode de défaillance
    # « crédible et faux » que cette colonne existe pour empêcher.
    pipeline_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)

    # Clé de COMPARABILITÉ : `<modèle_logique>@<digest8>` (ex. `legacy-768@0a109f42`),
    # dérivée des champs d'identité ATTESTÉS par la Gateway dans la réponse
    # (`model_logical`, `model_digest` vérifié) — jamais découpée de `pipeline_id`, qui
    # est opaque. Pourquoi une seconde colonne plutôt qu'un filtre sur `pipeline_id` :
    # la tâche fait partie de la transformation, si bien qu'un chunk et une question du
    # même modèle portent deux `pipeline_id` DIFFÉRENTS. Une égalité sur `pipeline_id`
    # ne remonterait donc JAMAIS rien. La famille est ce qui décide réellement de la
    # comparabilité, et elle est indexable.
    pipeline_family = models.CharField(max_length=191, blank=True, null=True)

    # provenance / metadata
    page_number = models.IntegerField(null=True, blank=True)
    meta = JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rag_chunk"
        indexes = [
            models.Index(fields=["rag_space", "document"]),
            models.Index(fields=["rag_space", "chunk_index"]),
            # Index du filtre de recherche gouvernée : `rag_space_id = … AND
            # pipeline_family = …`. Composite et dans cet ordre, parce que le verrou
            # d'espace est TOUJOURS présent et le plus sélectif.
            models.Index(
                fields=["rag_space", "pipeline_family"],
                name="rag_chunk_space_pipeline_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Chunk({self.document_id}, idx={self.chunk_index})"
