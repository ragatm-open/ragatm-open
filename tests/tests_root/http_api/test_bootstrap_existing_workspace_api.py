import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.workspaces.models import Workspace, Membership
from apps.rag_core.models import RagSpace

User = get_user_model()


@pytest.mark.django_db
def test_bootstrap_is_idempotent_when_user_already_has_workspace():
    u = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    ws = Workspace.objects.create(name="WS1")
    Membership.objects.create(workspace=ws, user=u, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(workspace=ws, name="RS1")

    client = APIClient()
    client.force_authenticate(user=u)

    res = client.post("/api/v1/bootstrap/")
    assert res.status_code == 200
    assert res.data["created"] is False
    assert res.data["workspace"]["id"] == ws.id
    assert res.data["workspace"]["role"] == Membership.ROLE_MEMBER
    assert str(res.data["ragspace"]["id"]) == str(rs.id)


@pytest.mark.django_db
def test_bootstrap_creates_default_ragspace_in_the_existing_workspace():
    """Cas NOMINAL : le workspace a un owner, donc un périmètre déterminable."""
    u = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    owner = User.objects.create_user(username="own", email="own@example.com", password="pw")
    ws = Workspace.objects.create(name="WS1")
    owner_m = Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    Membership.objects.create(
        workspace=ws, user=u, role=Membership.ROLE_MEMBER, owner_scope=owner_m
    )

    client = APIClient()
    client.force_authenticate(user=u)

    res = client.post("/api/v1/bootstrap/")
    assert res.status_code == 200
    assert res.data["created"] is True
    assert res.data["workspace"]["id"] == ws.id
    assert res.data["ragspace"]["name"] == "Default"
    assert RagSpace.objects.filter(workspace=ws).count() == 1
    # Le périmètre est celui du membre, résolu via son owner_scope.
    assert RagSpace.objects.get(workspace=ws).owner_scope_id == owner_m.pk


@pytest.mark.django_db
def test_bootstrap_does_not_create_an_unusable_space_in_an_ownerless_workspace():
    """Workspace SANS owner : aucun périmètre n'est déterminable.

    Créer l'espace quand même produirait un objet que personne ne pourrait ouvrir
    sous enforcement owner-scope (404 `ragspace_without_scope`). Promouvoir
    l'utilisateur owner pour s'en sortir élargirait ses droits. L'endpoint lui
    fournit donc un workspace NEUF dont il est légitimement owner — c'est sa raison
    d'être (« a minimal working environment »).

    Le workspace d'origine n'est pas touché : ni espace créé, ni rôle modifié.
    """
    u = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    ws = Workspace.objects.create(name="WS1")
    m = Membership.objects.create(workspace=ws, user=u, role=Membership.ROLE_MEMBER)

    client = APIClient()
    client.force_authenticate(user=u)

    res = client.post("/api/v1/bootstrap/")
    assert res.status_code == 200
    assert res.data["created"] is True
    assert res.data["workspace"]["id"] != ws.id
    assert res.data["workspace"]["role"] == Membership.ROLE_OWNER

    # Rien n'a été créé ni modifié dans le workspace d'origine.
    assert RagSpace.objects.filter(workspace=ws).count() == 0
    m.refresh_from_db()
    assert m.role == Membership.ROLE_MEMBER

    # Le nouvel espace porte bien un périmètre.
    new_space = RagSpace.objects.get(id=res.data["ragspace"]["id"])
    assert new_space.owner_scope_id is not None
    assert new_space.owner_scope.user_id == u.id
    assert new_space.owner_scope.role == Membership.ROLE_OWNER
