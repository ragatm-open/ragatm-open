"""La trace rerank (B4) est persistée dans RagQueryLog et exposée par query_rag."""
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.audit.models import RagQueryLog
from apps.rag_core.models import Chunk, Document, DocumentStatus, RagSpace
from apps.rag_core.services.query import query_rag
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _seed(n_chunks=8):
    ws = Workspace.objects.create(name="W")
    space = RagSpace.objects.create(name="S", workspace=ws)
    doc = Document.objects.create(rag_space=space, filename="d.pdf", status=DocumentStatus.READY)
    for i in range(n_chunks):
        Chunk.objects.create(
            rag_space=space,
            document=doc,
            chunk_index=i,
            text=f"La capitale de la France est Paris (extrait {i}).",
            page_number=1,
            meta={},
        )
    return space


@override_settings(RAG_RERANK_ENABLED=True, RAG_RERANK_PROVIDER="stub")
def test_rerank_info_persisted_when_triggered():
    space = _seed(n_chunks=8)
    user = get_user_model().objects.create_user(username="u", password="pw")

    out = query_rag(rag_space_id=space.id, question="Quelle est la capitale de la France ?", user=user)
    assert out["status"] == "ok"

    # trace exposée par le pipeline
    assert out["rerank"]["effective_provider"] == "stub"
    assert out["rerank"]["fallback_used"] is False
    assert out["rerank"]["n_in"] >= 6
    assert out["rerank"]["n_out"] >= 1

    # trace persistée en audit
    log = RagQueryLog.objects.get(id=out["log_id"])
    assert log.rerank.get("effective_provider") == "stub"
    assert log.rerank.get("n_in") >= 6
    # rerank_score additif dans les sources d'audit
    assert any("rerank_score" in s for s in log.sources)


@override_settings(RAG_RERANK_ENABLED=False)
def test_rerank_info_empty_when_disabled():
    space = _seed(n_chunks=3)
    user = get_user_model().objects.create_user(username="u2", password="pw")

    out = query_rag(rag_space_id=space.id, question="Quelle est la capitale de la France ?", user=user)
    assert out["status"] == "ok"
    assert out["rerank"] == {}

    log = RagQueryLog.objects.get(id=out["log_id"])
    assert log.rerank == {}
