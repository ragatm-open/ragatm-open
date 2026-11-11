import pytest
from apps.rag_core.services.query import query_rag

pytestmark = pytest.mark.django_db

def test_no_source_returns_no_answer():
    result = query_rag(rag_space_id=None, question="Question sans source")
    assert result["status"] == "no_answer"
    assert result["sources"] == []
