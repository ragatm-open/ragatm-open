"""Tests de l'API workspaces (list / members / garde-fou dernier owner)."""
import pytest

from apps.workspaces.models import Membership

pytestmark = pytest.mark.django_db


def test_list_workspaces(api_client, workspace):
    res = api_client.get("/api/v1/workspaces/")
    assert res.status_code == 200
    ids = [w["id"] for w in res.data]
    assert workspace.id in ids


def test_list_only_own_workspaces(api_client, workspace, foreign_setup):
    res = api_client.get("/api/v1/workspaces/")
    ids = [w["id"] for w in res.data]
    assert foreign_setup["workspace"].id not in ids


def test_members_requires_admin(api_client, workspace):
    # api_client est member -> 403
    res = api_client.get(f"/api/v1/workspaces/{workspace.id}/members/")
    assert res.status_code == 403


def test_members_list_as_admin(admin_client, workspace, user_with_email):
    res = admin_client.get(f"/api/v1/workspaces/{workspace.id}/members/")
    assert res.status_code == 200
    emails = [m["email"] for m in res.data]
    assert "u1@example.com" in emails


def test_post_members_is_deprecated_under_v2(admin_client, workspace):
    """INV-Z : sous le flux v2 (défaut), POST /members/ est neutralisé (409) et ne
    crée NI compte NI membership NI reset_link. L'invitation passe par /invitations/."""
    res = admin_client.post(
        f"/api/v1/workspaces/{workspace.id}/members/",
        {"email": "newbie@example.com", "role": "member"},
        format="json",
    )
    assert res.status_code == 409
    assert res.data["code"] == "use_invitations_endpoint"
    assert not Membership.objects.filter(
        workspace=workspace, user__email="newbie@example.com"
    ).exists()


def test_legacy_add_member_hides_reset_link_by_default(admin_client, workspace, settings):
    """Rollback (flag v2 off) : le chemin historique n'expose jamais le reset_link
    par défaut (ni en prod). Preuve que le rollback ne réactive pas la fuite."""
    settings.WORKSPACE_INVITATIONS_V2_ENABLED = False
    settings.INVITE_RETURN_RESET_LINK = False
    res = admin_client.post(
        f"/api/v1/workspaces/{workspace.id}/members/",
        {"email": "secret@example.com", "role": "member"},
        format="json",
    )
    assert res.status_code in (200, 201)
    assert "reset_link" not in res.data
    assert "email_error" not in res.data
    assert "email_sent" in res.data


def test_legacy_add_member_exposes_reset_link_only_when_flag_enabled(admin_client, workspace, settings):
    """Rollback + INVITE_RETURN_RESET_LINK=True (DEV/support local) : lien exposé."""
    settings.WORKSPACE_INVITATIONS_V2_ENABLED = False
    settings.INVITE_RETURN_RESET_LINK = True
    res = admin_client.post(
        f"/api/v1/workspaces/{workspace.id}/members/",
        {"email": "supported@example.com", "role": "member"},
        format="json",
    )
    assert res.status_code in (200, 201)
    assert "reset_link" in res.data
    assert "/reset-password?uid=" in res.data["reset_link"]


def test_cannot_demote_last_owner(admin_client, workspace, user):
    membership = Membership.objects.get(workspace=workspace, user=user)
    res = admin_client.patch(
        f"/api/v1/workspaces/{workspace.id}/members/{membership.id}/",
        {"role": "member"},
        format="json",
    )
    assert res.status_code == 400


def test_workspace_ragspaces_create_as_admin(admin_client, workspace):
    res = admin_client.post(
        f"/api/v1/workspaces/{workspace.id}/ragspaces/",
        {"name": "Nouvel espace"},
        format="json",
    )
    assert res.status_code == 201
    assert res.data["name"] == "Nouvel espace"
