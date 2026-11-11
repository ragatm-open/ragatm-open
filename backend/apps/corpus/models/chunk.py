import uuid

from django.db import models
from django.db.models import JSONField
from pgvector.django import VectorField

from apps.rag_core.models.chunk import EMBEDDING_VECTOR_DIM

from .corpus import DomainCorpus
from .document import DomainCorpusDocument


class DomainCorpusChunk(models.Model):
    """Chunk indexé d'un document de corpus métier.

    IMPORTANT : ``corpus`` est dupliqué ici (comme ``rag_space`` sur Chunk) pour
    garantir l'étanchéité par requête — le retrieval filtre TOUJOURS par corpus.
    Dimension d'embedding identique à rag_chunk (768) : un corpus partagé stocke
    ses embeddings UNE seule fois, jamais dupliqués par workspace consommateur.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    corpus = models.ForeignKey(
        DomainCorpus, on_delete=models.CASCADE, related_name="chunks"
    )
    document = models.ForeignKey(
        DomainCorpusDocument, on_delete=models.CASCADE, related_name="chunks"
    )

    chunk_index = models.IntegerField(default=0)

    text = models.TextField()
    embedding = VectorField(dimensions=EMBEDDING_VECTOR_DIM, null=True, blank=True)

    page_number = models.IntegerField(null=True, blank=True)
    meta = JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "domain_corpus_chunk"
        indexes = [
            models.Index(fields=["corpus", "document"]),
            models.Index(fields=["corpus", "chunk_index"]),
        ]

    def __str__(self) -> str:
        return f"CorpusChunk({self.document_id}, idx={self.chunk_index})"
