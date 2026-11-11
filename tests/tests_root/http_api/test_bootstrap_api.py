"""Tests de l'endpoint bootstrap (idempotent)."""
import pytest
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db


def test_bootstrap_requires_auth(db):
    assert APIClient().post("/api/v1/bootstrap/").status_code == 401


def test_bootstrap_creates_workspace_and_ragspace(user):
    client = APIClient()
    client.force_authenticate(user=user)
    res = client.post("/api/v1/bootstrap/")
    assert res.status_code == 200
    assert res.data["created"] is True
    assert res.data["workspace"]["role"] == Membership.ROLE_OWNER
    assert res.data["ragspace"]["name"] == "Default"
    assert Workspace.objects.filter(memberships__user=user).count() == 1
    assert RagSpace.objects.count() == 1


def test_bootstrap_is_idempotent(user):
    client = APIClient()
    client.force_authenticate(user=user)
    r1 = client.post("/api/v1/bootstrap/")
    r2 = client.post("/api/v1/bootstrap/")
    assert r1.data["created"] is True
    assert r2.data["created"] is False
    # pas de doublon
    assert Workspace.objects.filter(memberships__user=user).count() == 1
