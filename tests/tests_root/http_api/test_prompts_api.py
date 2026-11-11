"""Tests de l'API prompts (CRUD + activation + ACL)."""
import pytest

from apps.chat.models import PromptTemplate

pytestmark = pytest.mark.django_db


def test_list_requires_workspace_id(api_client):
    res = api_client.get("/api/v1/prompts/")
    assert res.status_code == 400


def test_create_prompt(api_client, workspace):
    res = api_client.post(
        "/api/v1/prompts/",
        {"workspace_id": workspace.id, "name": "tmpl", "content": "Bonjour {context}"},
        format="json",
    )
    assert res.status_code == 201
    assert res.data["name"] == "tmpl"
    assert res.data["version"] == 1


def test_create_increments_version(api_client, workspace):
    for _ in range(2):
        api_client.post(
            "/api/v1/prompts/",
            {"workspace_id": workspace.id, "name": "dup", "content": "x"},
            format="json",
        )
    versions = sorted(
        PromptTemplate.objects.filter(workspace=workspace, name="dup").values_list("version", flat=True)
    )
    assert versions == [1, 2]


def test_list_prompts(api_client, workspace):
    api_client.post(
        "/api/v1/prompts/",
        {"workspace_id": workspace.id, "name": "t", "content": "x"},
        format="json",
    )
    res = api_client.get(f"/api/v1/prompts/?workspace_id={workspace.id}")
    assert res.status_code == 200
    assert len(res.data) >= 1


def test_activate_prompt(api_client, workspace):
    create = api_client.post(
        "/api/v1/prompts/",
        {"workspace_id": workspace.id, "name": "act", "content": "x"},
        format="json",
    )
    pid = create.data["id"]
    res = api_client.post(f"/api/v1/prompts/{pid}/activate/")
    assert res.status_code == 200
    assert res.data["is_active"] is True


def test_viewer_cannot_create_prompt(viewer_setup):
    res = viewer_setup["client"].post(
        "/api/v1/prompts/",
        {"workspace_id": viewer_setup["workspace"].id, "name": "x", "content": "y"},
        format="json",
    )
    assert res.status_code == 403


def test_non_member_forbidden(api_client, foreign_setup):
    res = api_client.get(f"/api/v1/prompts/?workspace_id={foreign_setup['workspace'].id}")
    assert res.status_code == 403
