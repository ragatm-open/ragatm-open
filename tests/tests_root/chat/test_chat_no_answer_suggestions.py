"""F2 : les suggestions no_answer sont ASYNCHRONES — le flux SSE ferme immédiatement
après ``done`` (aucune génération LLM dans le worker HTTP), une tâche est enfilée."""
from __future__ import annotations

import time

import pytest

from apps.chat import tasks as chat_tasks
from apps.chat.services import chat_service as cs

pytestmark = pytest.mark.django_db


@pytest.fixture
def thread(db):
    from django.contrib.auth import get_user_model

    from apps.chat.models import ChatThread
    from apps.rag_core.models import RagSpace
    from apps.workspaces.models import Membership, Workspace

    user = get_user_model().objects.create_user(username="u", password="pw")
    ws = Workspace.objects.create(name="W")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(name="S", workspace=ws)
    th = ChatThread.objects.create(user=user, rag_space=rs)
    return user, rs, th


def _mock_no_answer(monkeypatch):
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(cs, "mode_b_enabled", lambda: False)
    monkeypatch.setattr(cs, "_run_retrieval", lambda **k: {"status": "no_answer", "sources": [], "log_id": None})


def _spy_delay(monkeypatch):
    calls = []
    monkeypatch.setattr(chat_tasks.generate_rephrasing_suggestions_task, "delay", lambda **kw: calls.append(kw))
    # garde-fou : la génération réelle ne doit JAMAIS tourner dans la requête
    gen_calls = {"n": 0}
    import apps.chat.services.rephrasing_suggestions as rs_mod
    monkeypatch.setattr(rs_mod, "generate_rephrasing_suggestions",
                        lambda **k: gen_calls.__setitem__("n", gen_calls["n"] + 1) or ["X ?"])
    return calls, gen_calls


def test_stream_closes_immediately_and_no_inline_generation(thread, monkeypatch, settings):
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = True
    user, rs, th = thread
    _mock_no_answer(monkeypatch)
    calls, gen_calls = _spy_delay(monkeypatch)

    t0 = time.perf_counter()
    events = list(cs.chat_query_stream(
        rag_space_id=str(rs.id), question="Q ?", user=user, rag_space=rs, thread_id=str(th.id), request_id="rid-1",
    ))
    elapsed = time.perf_counter() - t0

    names = [e["event"] for e in events]
    assert names[-1] == "done" and names.count("done") == 1
    # plus AUCUN événement rephrasing_suggestions sur ce flux (F2)
    assert "rephrasing_suggestions" not in names
    # la génération LLM n'a PAS tourné dans la requête
    assert gen_calls["n"] == 0
    # une tâche a été enfilée, avec des IDENTIFIANTS seulement (aucun prompt/question)
    assert len(calls) == 1
    assert calls[0] == {"thread_id": str(th.id), "request_id": "rid-1"}
    assert "Q ?" not in str(calls[0])
    # fermeture rapide du flux (pas de blocage worker)
    assert elapsed < 1.0


def test_flag_off_no_enqueue(thread, monkeypatch, settings):
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = False
    user, rs, th = thread
    _mock_no_answer(monkeypatch)
    calls, _ = _spy_delay(monkeypatch)

    events = list(cs.chat_query_stream(
        rag_space_id=str(rs.id), question="Q ?", user=user, rag_space=rs, thread_id=str(th.id), request_id="rid-2",
    ))
    assert events[-1]["event"] == "done"
    assert calls == []  # aucune tâche enfilée


def test_no_thread_no_enqueue(monkeypatch, settings):
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = True
    _mock_no_answer(monkeypatch)
    calls, _ = _spy_delay(monkeypatch)
    events = list(cs.chat_query_stream(rag_space_id="rs", question="Q ?", user=None, rag_space=None, request_id="r"))
    assert events[-1]["event"] == "done"
    assert calls == []  # pas de thread -> pas d'enqueue


def test_sync_no_answer_unchanged(monkeypatch, settings):
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = True
    _mock_no_answer(monkeypatch)
    out = cs.chat_query(rag_space_id="rs", question="Q ?", user=None, rag_space=None)
    assert out["status"] == "no_answer" and out["suggested_rephrasings"] == []
