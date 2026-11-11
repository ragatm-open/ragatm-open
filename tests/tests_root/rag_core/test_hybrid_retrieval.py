import pytest
from apps.rag_core.models import RagSpace
from apps.rag_core.services.ingest import ingest_plain_text
from apps.rag_core.services.query import query_rag

pytestmark = pytest.mark.django_db

def test_hybrid_retrieval_works_with_embeddings(rag_space):
    space = rag_space

    ingest_plain_text(
        rag_space_id=space.id,
        filename="doc.txt",
        text="Paris est la capitale de la France.",
    )

    result = query_rag(
        rag_space_id=space.id,
        question="capitale france",
    )

    assert result["status"] == "ok"
    assert len(result["sources"]) >= 1
    assert "Paris" in result["sources"][0]["text"]
