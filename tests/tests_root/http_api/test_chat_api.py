"""Tests de l'API chat (POST /chat/, threads, feedback)."""
import pytest

from apps.audit.models import RagQueryLog
from apps.chat.models import ChatThread

pytestmark = pytest.mark.django_db


def test_chat_requires_auth(db, rag_space):
    from rest_framework.test import APIClient

    res = APIClient().post(
        "/api/v1/chat/", {"rag_space_id": str(rag_space.id), "question": "x"}, format="json"
    )
    assert res.status_code == 401


def test_chat_missing_fields(api_client):
    res = api_client.post("/api/v1/chat/", {"question": "x"}, format="json")
    assert res.status_code == 400


def test_chat_no_answer_when_empty_space(api_client, rag_space):
    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(rag_space.id), "question": "Quelle est la capitale ?"},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["status"] == "no_answer"
    assert res.data["sources"] == []
    # un log d'audit a été créé
    assert RagQueryLog.objects.filter(rag_space=rag_space).count() == 1


def test_create_and_list_threads(api_client, rag_space):
    create = api_client.post(
        "/api/v1/chat/threads/",
        {"rag_space_id": str(rag_space.id), "title": "Ma conversation"},
        format="json",
    )
    assert create.status_code == 201
    thread_id = create.data["id"]

    listing = api_client.get(f"/api/v1/chat/threads/?rag_space_id={rag_space.id}")
    assert listing.status_code == 200
    assert any(t["id"] == thread_id for t in listing.data)


def test_threads_requires_rag_space_id(api_client):
    res = api_client.get("/api/v1/chat/threads/")
    assert res.status_code == 400


def test_feedback_invalid_score(api_client):
    res = api_client.post(
        "/api/v1/feedback/", {"log_id": "00000000-0000-0000-0000-000000000000", "score": 9},
        format="json",
    )
    assert res.status_code == 400


def test_feedback_log_not_found(api_client):
    res = api_client.post(
        "/api/v1/feedback/",
        {"log_id": "00000000-0000-0000-0000-000000000000", "score": 1},
        format="json",
    )
    assert res.status_code == 404


def test_feedback_updates_own_log(api_client, user, rag_space, workspace):
    log = RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space, question="q", answer="a", status="ok"
    )
    res = api_client.post(
        "/api/v1/feedback/", {"log_id": str(log.id), "score": 1, "reason": "incomplete"},
        format="json",
    )
    assert res.status_code == 200
    log.refresh_from_db()
    assert log.feedback_score == 1


def test_cannot_feedback_other_users_log(api_client, other_user, rag_space, workspace):
    log = RagQueryLog.objects.create(
        user=other_user, workspace=workspace, rag_space=rag_space, question="q", answer="a", status="ok"
    )
    res = api_client.post(
        "/api/v1/feedback/", {"log_id": str(log.id), "score": -1}, format="json"
    )
    assert res.status_code == 404
