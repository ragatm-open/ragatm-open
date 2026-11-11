"""F2 : la tâche Celell de suggestions — pending->ready/failed, idempotence, anti-fuite."""
from __future__ import annotations

import logging

import pytest

from apps.chat import tasks as chat_tasks
from apps.chat.models import RephrasingSuggestion

pytestmark = pytest.mark.django_db

SECRET = "SECRET_SENTINEL_DO_NOT_LOG"


@pytest.fixture
def ctx(db, settings):
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = True
    from django.contrib.auth import get_user_model

    from apps.chat.models import ChatMessage, ChatThread
    from apps.rag_core.models import RagSpace
    from apps.workspaces.models import Membership, Workspace

    user = get_user_model().objects.create_user(username="u", password="pw")
    ws = Workspace.objects.create(name="W")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(name="S", workspace=ws)
    th = ChatThread.objects.create(user=user, rag_space=rs)
    # question utilisateur contenant le SECRET (contenu métier)
    ChatMessage.objects.create(thread=th, role="user", content=f"Question {SECRET} ?", request_id="rid")
    return th


def _run(th):
    # exécution directe de la fonction de la tâche (équivalent eager)
    return chat_tasks.generate_rephrasing_suggestions_task.run(thread_id=str(th.id), request_id="rid")


def test_task_success_pending_to_ready(ctx, monkeypatch):
    import apps.chat.services.rephrasing_suggestions as rs_mod
    monkeypatch.setattr(rs_mod, "generate_rephrasing_suggestions", lambda **k: ["Reformulation A ?", "Reformulation B ?"])

    out = _run(ctx)
    assert out["ok"] is True
    row = RephrasingSuggestion.objects.get(thread=ctx, request_id="rid")
    assert row.status == "ready"
    assert row.suggestions == ["Reformulation A ?", "Reformulation B ?"]


def test_task_failure_pending_to_failed_no_secret(ctx, monkeypatch, caplog):
    """Exception provider INATTENDUE -> failed + log ERROR avec stack trace, sans contenu."""
    import apps.chat.services.rephrasing_suggestions as rs_mod

    def boom(**k):
        raise RuntimeError("provider down")  # message dev, sans contenu

    monkeypatch.setattr(rs_mod, "generate_rephrasing_suggestions", boom)

    with caplog.at_level(logging.ERROR, logger="apps.chat.tasks"):
        out = _run(ctx)  # Option A : la tâche ne relance PAS, elle absorbe l'échec
    assert out["ok"] is False and out["reason"] == "generation_error"
    row = RephrasingSuggestion.objects.get(thread=ctx, request_id="rid")
    assert row.status == "failed" and row.error_code == "generation_error"

    rec = [r for r in caplog.records if "event=rephrasing_suggestions_failed" in r.getMessage()]
    assert len(rec) == 1
    assert rec[0].levelno == logging.ERROR
    assert rec[0].exc_info is not None  # stack trace conservée (inattendu)
    assert "error_code=generation_error" in rec[0].getMessage()
    assert SECRET not in caplog.text  # ni question, ni prompt, ni suggestion


def test_task_timeout_is_categorized_without_stacktrace(ctx, monkeypatch, caplog):
    """Timeout = échec ATTENDU et catégorisé -> ERROR, error_code borné, sans stack trace."""
    from celery.exceptions import SoftTimeLimitExceeded

    import apps.chat.services.rephrasing_suggestions as rs_mod

    def slow(**k):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(rs_mod, "generate_rephrasing_suggestions", slow)

    with caplog.at_level(logging.ERROR, logger="apps.chat.tasks"):
        out = _run(ctx)
    assert out == {"ok": False, "reason": "timeout"}
    row = RephrasingSuggestion.objects.get(thread=ctx, request_id="rid")
    assert row.status == "failed" and row.error_code == "timeout"

    rec = [r for r in caplog.records if "event=rephrasing_suggestions_failed" in r.getMessage()]
    assert len(rec) == 1 and rec[0].levelno == logging.ERROR
    assert rec[0].exc_info is None  # attendu -> pas de stack trace (pas de bruit)
    assert "error_code=timeout" in rec[0].getMessage()
    assert SECRET not in caplog.text


def test_ready_log_counts_only_never_content(ctx, monkeypatch, caplog):
    import apps.chat.services.rephrasing_suggestions as rs_mod
    monkeypatch.setattr(rs_mod, "generate_rephrasing_suggestions", lambda **k: [f"Reformuler {SECRET} ?"])

    with caplog.at_level(logging.INFO, logger="apps.chat.tasks"):
        _run(ctx)

    msg = caplog.text
    assert "event=rephrasing_suggestions_ready" in msg and "count=1" in msg
    assert SECRET not in msg  # la suggestion générée elle-même n'est jamais loguée


def test_operator_can_count_repeated_failures(ctx, monkeypatch, caplog):
    """Panne globale du provider : chaque échec émet un événement comptable (logs = mécanisme
    d'observabilité retenu, le dépôt n'ayant pas d'infra de métriques)."""
    import apps.chat.services.rephrasing_suggestions as rs_mod
    from apps.chat.models import ChatMessage

    def boom(**k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(rs_mod, "generate_rephrasing_suggestions", boom)

    with caplog.at_level(logging.ERROR, logger="apps.chat.tasks"):
        for rid in ("rid", "rid-2", "rid-3"):
            ChatMessage.objects.get_or_create(
                thread=ctx, role="user", request_id=rid, defaults={"content": f"Q {SECRET} ?"}
            )
            chat_tasks.generate_rephrasing_suggestions_task.run(thread_id=str(ctx.id), request_id=rid)

    failures = [r for r in caplog.records if "event=rephrasing_suggestions_failed" in r.getMessage()]
    assert len(failures) == 3  # un opérateur compte les échecs par event=
    assert RephrasingSuggestion.objects.filter(thread=ctx, status="failed").count() == 3
    assert SECRET not in caplog.text


def test_task_idempotent_single_generation(ctx, monkeypatch):
    import apps.chat.services.rephrasing_suggestions as rs_mod
    calls = {"n": 0}
    monkeypatch.setattr(
        rs_mod, "generate_rephrasing_suggestions",
        lambda **k: calls.__setitem__("n", calls["n"] + 1) or ["A ?"],
    )
    _run(ctx)          # 1re exécution -> ready
    out2 = _run(ctx)   # 2e exécution -> déjà ready, aucune régénération
    assert out2 == {"ok": True, "reason": "already_ready"}
    assert calls["n"] == 1
    assert RephrasingSuggestion.objects.filter(thread=ctx, request_id="rid").count() == 1


def test_task_flag_off_no_generation(ctx, monkeypatch, settings):
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = False
    called = {"n": 0}
    import apps.chat.services.rephrasing_suggestions as rs_mod
    monkeypatch.setattr(rs_mod, "generate_rephrasing_suggestions", lambda **k: called.__setitem__("n", 1) or [])
    out = _run(ctx)
    assert out == {"ok": False, "reason": "disabled"}
    assert called["n"] == 0


def test_task_bounds_no_retry():
    t = chat_tasks.generate_rephrasing_suggestions_task
    assert t.max_retries == 0
    assert t.soft_time_limit == 5 and t.time_limit == 8
