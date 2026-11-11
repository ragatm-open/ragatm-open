"""A3 — verrouillage de la dimension d'embeddings.

La colonne pgvector est figée (768). On vérifie que le system check
`rag_core.E001` détecte toute divergence de `RAG_EMBEDDING_DIM`.
"""

from django.test import override_settings

from apps.rag_core.apps import check_embedding_dim
from apps.rag_core.models.chunk import Chunk


def _ids():
    return {e.id for e in check_embedding_dim(None)}


def test_chunk_embedding_dimension_is_locked_to_768():
    assert Chunk._meta.get_field("embedding").dimensions == 768


@override_settings(RAG_EMBEDDING_DIM=768)
def test_check_passes_when_setting_matches_column():
    assert "rag_core.E001" not in _ids()


@override_settings(RAG_EMBEDDING_DIM=1536)
def test_check_fails_when_setting_diverges():
    assert "rag_core.E001" in _ids()


@override_settings(RAG_EMBEDDING_DIM="not-an-int")
def test_check_fails_on_non_integer_setting():
    assert "rag_core.E002" in _ids()
