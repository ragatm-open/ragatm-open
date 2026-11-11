import pytest
from apps.rag_core.models import RagSpace, Document, DocumentStatus, Chunk
from apps.rag_core.services.query import query_rag

pytestmark = pytest.mark.django_db

def test_rag_space_isolation_never_leaks_chunks():
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(name="W1")
    space_a = RagSpace.objects.create(name="A", workspace=ws)
    space_b = RagSpace.objects.create(name="B", workspace=ws)


    doc_a = Document.objects.create(rag_space=space_a, filename="a.pdf", status=DocumentStatus.READY)
    doc_b = Document.objects.create(rag_space=space_b, filename="b.pdf", status=DocumentStatus.READY)

    # Chunk pertinent mais dans l'autre espace
    Chunk.objects.create(
        rag_space=space_b,
        document=doc_b,
        chunk_index=0,
        text="Paris est la capitale de la France.",
        page_number=1,
        meta={},
    )

    # Dans A, rien ne doit remonter
    result = query_rag(rag_space_id=space_a.id, question="capitale france")
    assert result["status"] == "no_answer"
    assert result["sources"] == []

    # Dans B, ça doit remonter
    result_b = query_rag(rag_space_id=space_b.id, question="capitale france")
    assert result_b["status"] == "ok"
    assert len(result_b["sources"]) >= 1
