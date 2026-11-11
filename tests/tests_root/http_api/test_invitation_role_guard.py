"""INV-A0 — Gardes d'attribution du rôle owner (défense en profondeur backend).

Le rôle propriétaire (`owner`) ne doit JAMAIS pouvoir être attribué via les flux
de gestion des membres ou d'invitation :
- création/ajout d'un membre `owner` ;
- promotion d'un membre existant vers `owner` ;
- auto-promotion d'un administrateur vers `owner`.

Ces gardes sont côté backend (serializer + vue), pas seulement côté frontend.
Les règles historiques (protection du dernier owner) restent inchangées.
"""
import pytest
from rest_framework.test import APIClient

from apps.workspaces.models import Membership, Workspace


def _admin_client(db):
    """Un utilisateur ADMIN (≠ owner) sur un workspace, authentifié."""
    from django.contrib.auth import get_user_model

    U = get_user_model()
    owner = U.objects.create_user(username="owner1", password="pw", email="owner1@e.com")
    admin = U.objects.create_user(username="admin1", password="pw", email="admin1@e.com")
    ws = Workspace.objects.create(name="WGuard")
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    Membership.objects.create(workspace=ws, user=admin, role=Membership.ROLE_ADMIN)
    c = APIClient()
    c.force_authenticate(user=admin)
    return {"client": c, "workspace": ws, "admin": admin, "owner": owner}


@pytest.mark.django_db
def test_admin_invite_owner_is_forbidden():
    """Le flux d'invitation v2 refuse le rôle owner (serializer)."""
    from apps.workspaces.models import WorkspaceInvitation

    s = _admin_client(None)
    resp = s["client"].post(
        f"/api/v1/workspaces/{s['workspace'].id}/invitations/",
        {"email": "newowner@e.com", "role": "owner"},
        format="json",
    )
    assert resp.status_code == 400, resp.content
    # Aucune invitation ni appartenance owner n'a été créée.
    assert not WorkspaceInvitation.objects.filter(
        workspace=s["workspace"], email_normalized="newowner@e.com"
    ).exists()
    assert not Membership.objects.filter(
        workspace=s["workspace"], role=Membership.ROLE_OWNER, user__email="newowner@e.com"
    ).exists()


@pytest.mark.django_db
def test_owner_invitation_role_is_forbidden():
    """Même un owner ne peut pas attribuer owner via le flux d'invitation."""
    from django.contrib.auth import get_user_model

    U = get_user_model()
    owner = U.objects.create_user(username="owner2", password="pw", email="owner2@e.com")
    ws = Workspace.objects.create(name="WGuard2")
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    c = APIClient()
    c.force_authenticate(user=owner)
    resp = c.post(
        f"/api/v1/workspaces/{ws.id}/invitations/",
        {"email": "second-owner@e.com", "role": "owner"},
        format="json",
    )
    assert resp.status_code == 400, resp.content


@pytest.mark.django_db
def test_admin_promote_member_to_owner_is_forbidden():
    s = _admin_client(None)
    from django.contrib.auth import get_user_model

    U = get_user_model()
    victim = U.objects.create_user(username="victim", password="pw", email="victim@e.com")
    m = Membership.objects.create(
        workspace=s["workspace"], user=victim, role=Membership.ROLE_MEMBER
    )
    resp = s["client"].patch(
        f"/api/v1/workspaces/{s['workspace'].id}/members/{m.id}/",
        {"role": "owner"},
        format="json",
    )
    assert resp.status_code in (400, 403), resp.content
    m.refresh_from_db()
    assert m.role == Membership.ROLE_MEMBER


@pytest.mark.django_db
def test_admin_self_promote_to_owner_is_forbidden():
    s = _admin_client(None)
    m = Membership.objects.get(workspace=s["workspace"], user=s["admin"])
    resp = s["client"].patch(
        f"/api/v1/workspaces/{s['workspace'].id}/members/{m.id}/",
        {"role": "owner"},
        format="json",
    )
    assert resp.status_code in (400, 403), resp.content
    m.refresh_from_db()
    assert m.role == Membership.ROLE_ADMIN
