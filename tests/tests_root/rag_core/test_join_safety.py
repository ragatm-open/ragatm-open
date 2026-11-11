import pytest
from apps.rag_core.models import RagSpace, Document, DocumentStatus, Chunk
from apps.rag_core.services.query import query_rag

@pytest.mark.django_db
def test_join_safety_blocks_cross_space_document_metadata():
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.create(name="W1")
    a = RagSpace.objects.create(name="A", workspace=ws)
    b = RagSpace.objects.create(name="B", workspace=ws)


    # Document dans B
    doc_b = Document.objects.create(
        rag_space=b,
        filename="secret.txt",
        status=DocumentStatus.READY,
        content_type="text/plain",
        size_bytes=1,
    )

    # Chunk volontairement incohérent: rag_space A mais document = doc_b (B)
    Chunk.objects.create(
        rag_space=a,
        document=doc_b,
        chunk_index=0,
        text="Paris est la capitale de la France",
        embedding=[0.0] * 768,
    )

    res = query_rag(rag_space_id=a.id, question="capitale france")

    # Si le JOIN est bien verrouillé, aucun chunk ne remonte
    assert res["status"] == "no_answer"
    assert res["sources"] == []
