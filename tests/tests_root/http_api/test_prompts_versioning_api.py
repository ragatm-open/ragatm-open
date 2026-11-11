import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.workspaces.models import Workspace, Membership
from apps.chat.models import PromptTemplate

User = get_user_model()


@pytest.mark.django_db
def test_prompts_create_versions_and_activate():
    u = User.objects.create_user(username="u1", password="pw")
    ws = Workspace.objects.create(name="W1")
    Membership.objects.create(workspace=ws, user=u, role=Membership.ROLE_MEMBER)

    client = APIClient()
    client.force_authenticate(user=u)

    r1 = client.post(
        "/api/v1/prompts/",
        {"workspace_id": str(ws.id), "name": "default", "content": "v1 {question} {context}", "activate": True},
        format="json",
    )
    
    print("status:", r1.status_code)
    print("data:", r1.data)
    print("raw:", getattr(r1, "content", b"")[:500])
    
    assert r1.status_code == 201
    assert r1.data["version"] == 1

    r2 = client.post(
        "/api/v1/prompts/",
        {"workspace_id": str(ws.id), "name": "default", "content": "v2 {question} {context}", "activate": False},
        format="json",
    )
    assert r2.status_code == 201
    assert r2.data["version"] == 2

    tpl2_id = r2.data["id"]
    r_act = client.post(f"/api/v1/prompts/{tpl2_id}/activate/", {}, format="json")
    assert r_act.status_code == 200

    # une seule active
    assert PromptTemplate.objects.filter(workspace=ws, name="default", is_active=True).count() == 1
    active = PromptTemplate.objects.get(workspace=ws, name="default", is_active=True)
    assert active.version == 2


@pytest.mark.django_db
def test_prompts_viewer_forbidden_create():
    u = User.objects.create_user(username="u1", password="pw")
    ws = Workspace.objects.create(name="W1")
    Membership.objects.create(workspace=ws, user=u, role=Membership.ROLE_VIEWER)

    client = APIClient()
    client.force_authenticate(user=u)

    r = client.post(
        "/api/v1/prompts/",
        {"workspace_id": str(ws.id), "name": "default", "content": "x", "activate": False},
        format="json",
    )
    assert r.status_code == 403
