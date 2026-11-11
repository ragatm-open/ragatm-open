"""F2 : endpoint GET /chat/threads/<id>/rephrasing-suggestions/ — statuts + permissions."""
from __future__ import annotations

import pytest

from apps.chat.models import ChatThread, RephrasingSuggestion

pytestmark = pytest.mark.django_db


def _thread(user, rag_space):
    return ChatThread.objects.create(user=user, rag_space=rag_space)


def _url(tid, rid=None):
    u = f"/api/v1/chat/threads/{tid}/rephrasing-suggestions/"
    return f"{u}?request_id={rid}" if rid else u


def test_none_when_absent(api_client, user, rag_space):
    th = _thread(user, rag_space)
    r = api_client.get(_url(th.id))
    assert r.status_code == 200 and r.json() == {"status": "none", "suggestions": []}


def test_pending_then_ready(api_client, user, rag_space):
    th = _thread(user, rag_space)
    RephrasingSuggestion.objects.create(thread=th, request_id="r1", status="pending")
    assert api_client.get(_url(th.id, "r1")).json() == {"status": "pending", "suggestions": []}

    RephrasingSuggestion.objects.filter(thread=th, request_id="r1").update(
        status="ready", suggestions=["A ?", "B ?"]
    )
    assert api_client.get(_url(th.id, "r1")).json() == {"status": "ready", "suggestions": ["A ?", "B ?"]}


def test_failed_hides_internal_error(api_client, user, rag_space):
    th = _thread(user, rag_space)
    RephrasingSuggestion.objects.create(thread=th, request_id="r2", status="failed", error_code="generation_error", suggestions=[])
    body = api_client.get(_url(th.id, "r2")).json()
    assert body == {"status": "failed", "suggestions": []}
    assert "error_code" not in body and "generation_error" not in str(body)


def test_unknown_request_id_is_none_not_leaky(api_client, user, rag_space):
    """request_id inconnu -> `none` (indiscernable d'un tour sans suggestions)."""
    th = _thread(user, rag_space)
    RephrasingSuggestion.objects.create(thread=th, request_id="r1", status="ready", suggestions=["A ?"])
    body = api_client.get(_url(th.id, "inconnu")).json()
    assert body == {"status": "none", "suggestions": []}  # aucune fuite du tour voisin


def test_permission_other_user_is_not_found(api_client, rag_space, django_user_model):
    # thread appartenant à un AUTRE utilisateur -> 404 anti-énumération
    other = django_user_model.objects.create_user(username="other", password="pw")
    th = _thread(other, rag_space)
    RephrasingSuggestion.objects.create(thread=th, request_id="r1", status="ready", suggestions=["FUITE ?"])
    r = api_client.get(_url(th.id, "r1"))
    assert r.status_code == 404
    assert "FUITE" not in r.content.decode()  # aucune suggestion pour un non-autorisé


def test_permission_thread_of_other_workspace_is_not_found(api_client, django_user_model):
    """Thread d'un autre workspace (donc d'un autre user) -> 404, comme un thread inexistant :
    l'existence n'est pas distinguable."""
    from apps.rag_core.models import RagSpace
    from apps.workspaces.models import Membership, Workspace

    other = django_user_model.objects.create_user(username="other-ws", password="pw")
    ws2 = Workspace.objects.create(name="W2")
    Membership.objects.create(workspace=ws2, user=other, role=Membership.ROLE_MEMBER)
    rs2 = RagSpace.objects.create(name="space-b", workspace=ws2)
    th = _thread(other, rs2)

    assert api_client.get(_url(th.id)).status_code == 404


def test_permission_owner_without_workspace_membership_is_denied(api_client, user, rag_space):
    """Propriétaire du thread mais accès RagSpace révoqué -> refus (require_rag_space_access),
    exactement comme l'endpoint de référence /messages/."""
    from apps.workspaces.models import Membership

    th = _thread(user, rag_space)
    RephrasingSuggestion.objects.create(thread=th, request_id="r1", status="ready", suggestions=["FUITE ?"])
    Membership.objects.filter(workspace=rag_space.workspace, user=user).delete()

    r = api_client.get(_url(th.id, "r1"))
    assert r.status_code == 403  # convention de require_rag_space_access (PermissionDenied)
    assert "FUITE" not in r.content.decode()


def test_policy_matches_reference_messages_endpoint(api_client, user, rag_space, django_user_model):
    """Parité stricte avec l'endpoint qui fait autorité (GET .../messages/) : même code sur
    thread possédé, thread d'autrui et thread inexistant. Empêche toute divergence future."""
    import uuid

    other = django_user_model.objects.create_user(username="other2", password="pw")
    mine = _thread(user, rag_space)
    theirs = _thread(other, rag_space)
    ghost = uuid.uuid4()

    for tid in (mine.id, theirs.id, ghost):
        ref = api_client.get(f"/api/v1/chat/threads/{tid}/messages/")
        f2 = api_client.get(_url(tid))
        assert f2.status_code == ref.status_code, f"divergence de politique sur {tid}"


def test_nonexistent_thread_is_not_found(api_client):
    import uuid
    r = api_client.get(_url(uuid.uuid4()))
    assert r.status_code == 404


def test_requires_auth(rag_space, user):
    from rest_framework.test import APIClient
    th = _thread(user, rag_space)
    r = APIClient().get(_url(th.id))  # non authentifié
    assert r.status_code in (401, 403)
