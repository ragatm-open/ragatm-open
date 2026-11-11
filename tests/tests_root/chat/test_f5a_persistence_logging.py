"""F5-A : un échec de persistance d'un message de chat est JOURNALISÉ (sans contenu),
la réponse reste délivrée, l'ordre SSE est intact, aucun secret dans les logs."""
from __future__ import annotations

import logging

import pytest

from apps.chat.services import chat_service as cs

pytestmark = pytest.mark.django_db

SECRET = "SECRET_SENTINEL_DO_NOT_LOG"


@pytest.fixture
def thread_ctx(db):
    from django.contrib.auth import get_user_model

    from apps.chat.models import ChatThread
    from apps.rag_core.models import RagSpace
    from apps.workspaces.models import Membership, Workspace

    user = get_user_model().objects.create_user(username="u", password="pw")
    ws = Workspace.objects.create(name="W")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(name="S", workspace=ws)
    return user, rs


def _mock_no_answer(monkeypatch):
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(cs, "mode_b_enabled", lambda: False)
    # no_answer AVEC contenu secret dans la réponse (pour le contrôle de fuite)
    # `_no_sources_answer` reçoit la langue de réponse résolue (`fr`/`en`/None) : le
    # double doit l'accepter, sinon il masque la signature réelle.
    monkeypatch.setattr(
        cs, "_no_sources_answer", lambda answer_language=None: f"Pas assez d'infos {SECRET}"
    )
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: {"status": "no_answer", "sources": [{"chunk_id": "c", "text": SECRET}], "log_id": None},
    )


def _flaky_assistant_append(monkeypatch):
    """append_message échoue UNIQUEMENT pour le message assistant (l'utilisateur passe).
    L'exception est une erreur DB réaliste (sans contenu) ; le SECRET reste dans le
    CONTENU du message (jamais transmis au logger)."""
    orig = cs.append_message

    def flaky(**kw):
        if kw.get("role") == "assistant":
            raise RuntimeError("db connection lost")  # erreur DB réaliste, sans contenu
        return orig(**kw)

    monkeypatch.setattr(cs, "append_message", flaky)


def test_assistant_persist_failure_is_logged_without_content(thread_ctx, monkeypatch, caplog):
    user, rs = thread_ctx
    _mock_no_answer(monkeypatch)
    _flaky_assistant_append(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="apps.chat.services.chat_service"):
        events = list(cs.chat_query_stream(
            rag_space_id=str(rs.id), question="Q ?", user=user, rag_space=rs, request_id="rid-1",
        ))

    # 1) l'échec est journalisé de façon corrélable
    assert "chat_message_persist_failed" in caplog.text
    assert "role=assistant" in caplog.text
    assert "stage=" in caplog.text and "request_id=rid-1" in caplog.text
    # 2) AUCUN contenu / secret dans les logs (ni contenu du message, ni exception)
    assert SECRET not in caplog.text
    # 3) la réponse reste délivrée + ordre SSE intact (un seul done)
    names = [e["event"] for e in events]
    assert names[-1] == "done" and names.count("done") == 1
    assert "token" in names or names[0] == "meta"


def test_no_crash_and_stream_closes_on_persist_failure(thread_ctx, monkeypatch):
    user, rs = thread_ctx
    _mock_no_answer(monkeypatch)
    _flaky_assistant_append(monkeypatch)
    # le générateur se consomme entièrement, sans exception qui remonte
    events = list(cs.chat_query_stream(
        rag_space_id=str(rs.id), question="Q ?", user=user, rag_space=rs, request_id="rid-2",
    ))
    assert events and events[-1]["event"] == "done"


def test_log_persist_helper_never_logs_content(caplog):
    class _T:
        id = "thread-xyz"

    with caplog.at_level(logging.ERROR, logger="apps.chat.services.chat_service"):
        cs._log_persist_failure(thread=_T(), role="assistant", stage="standard", request_id="r9")
    assert "thread_id=thread-xyz" in caplog.text and "stage=standard" in caplog.text
