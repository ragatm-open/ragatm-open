import pytest
from apps.rag_core.models import RagSpace, Document, DocumentStatus, Chunk
from apps.rag_core.services.query import query_rag

pytestmark = pytest.mark.django_db

def test_retrieval_returns_sources_when_match():
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(name="W1")
    space = RagSpace.objects.create(name="S1", workspace=ws)

    doc = Document.objects.create(rag_space=space, filename="doc1.pdf", status=DocumentStatus.READY)
    Chunk.objects.create(
        rag_space=space,
        document=doc,
        chunk_index=0,
        text="Paris est la capitale de la France.",
        page_number=1,
        meta={},
    )

    result = query_rag(rag_space_id=space.id, question="Quelle est la capitale de la France ?")
    assert result["status"] == "ok"
    assert len(result["sources"]) >= 1
    assert "Paris" in result["sources"][0]["text"]

def test_retrieval_no_answer_when_no_match():
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(name="W1")
    space = RagSpace.objects.create(name="S1", workspace=ws)
    doc = Document.objects.create(rag_space=space, filename="doc1.pdf", status=DocumentStatus.READY)
    Chunk.objects.create(
        rag_space=space,
        document=doc,
        chunk_index=0,
        text="Ce texte parle uniquement de tomates.",
        page_number=1,
        meta={},
    )

    result = query_rag(rag_space_id=space.id, question="Quelle est la capitale de la France ?")
    assert result["status"] == "no_answer"
    assert result["sources"] == []
