"""Tests de la config FTS configurable (B3) : whitelist + stemming french vs simple."""
import pytest
from django.test import override_settings

from apps.rag_core.models import Chunk, Document, DocumentStatus, RagSpace
from apps.rag_core.repositories.chunk_repository import fts_config, search_chunks_hybrid
from apps.workspaces.models import Workspace


@override_settings(RAG_FTS_CONFIG="french")
def test_fts_config_accepts_french():
    assert fts_config() == "french"


@override_settings(RAG_FTS_CONFIG="simple")
def test_fts_config_accepts_simple():
    assert fts_config() == "simple"


@override_settings(RAG_FTS_CONFIG="klingon")
def test_fts_config_rejects_unknown_falls_back_simple():
    # fail-safe : valeur hors whitelist -> simple (jamais d'injection regconfig)
    assert fts_config() == "simple"


@pytest.mark.django_db
def test_french_stemming_matches_plural_where_simple_does_not():
    ws = Workspace.objects.create(name="W")
    space = RagSpace.objects.create(name="S", workspace=ws)
    doc = Document.objects.create(rag_space=space, filename="d.pdf", status=DocumentStatus.READY)
    Chunk.objects.create(
        rag_space=space,
        document=doc,
        chunk_index=0,
        text="Les chevaux galopent dans le pré.",
        page_number=1,
        meta={},
    )

    # 'simple' : "cheval:*" (prefix) ne matche PAS le token "chevaux".
    with override_settings(RAG_FTS_CONFIG="simple"):
        simple_hits = search_chunks_hybrid(
            rag_space_id=space.id, question="cheval", top_k=5, min_text_rank=0.0
        )
    assert simple_hits == []

    # 'french' : stemming chevaux -> cheval -> match.
    with override_settings(RAG_FTS_CONFIG="french"):
        french_hits = search_chunks_hybrid(
            rag_space_id=space.id, question="cheval", top_k=5, min_text_rank=0.0
        )
    assert len(french_hits) == 1
    assert "chevaux" in french_hits[0]["text"]
