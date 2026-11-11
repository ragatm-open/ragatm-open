"""P1 — cycle de vie utilisateurs & memberships (plateforme, superuser only)."""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db
H = {"HTTP_HOST": "localhost"}


def _cli(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def su(db):
    return User.objects.create_user("p1_su", "p1_su@t.local", "x", is_superuser=True, is_staff=True)


# --- Désactivation / réactivation utilisateur -------------------------------

def test_user_disable_enable(su):
    t = User.objects.create_user("p1_t", "p1_t@t.local", "x")
    assert _cli(su).post(f"/api/v1/platform/users/{t.id}/disable/", {}, format="json", **H).status_code == 400
    r = _cli(su).post(f"/api/v1/platform/users/{t.id}/disable/", {"reason": "spam"}, format="json", **H)
    assert r.status_code == 200 and r.data["code"] == "user_disabled"
    t.refresh_from_db()
    assert t.is_active is False and User.objects.filter(pk=t.pk).exists()  # pas de hard-delete
    assert _cli(su).post(f"/api/v1/platform/users/{t.id}/disable/", {"reason": "x"}, format="json", **H).data["code"] == "already_user_disabled"
    assert _cli(su).post(f"/api/v1/platform/users/{t.id}/enable/", {"reason": "ok"}, format="json", **H).data["code"] == "user_enabled"


def test_last_active_superuser_protected(su):
    # su est l'unique superuser actif (DB de test vide) → sa désactivation est refusée.
    r = _cli(su).post(f"/api/v1/platform/users/{su.id}/disable/", {"reason": "x", "confirm_self": True}, format="json", **H)
    assert r.status_code == 409 and r.data["detail"] == "last_active_superuser"


def test_self_disable_requires_confirm(su):
    User.objects.create_user("p1_su2", "p1_su2@t.local", "x", is_superuser=True)  # pour ne pas être le dernier
    r = _cli(su).post(f"/api/v1/platform/users/{su.id}/disable/", {"reason": "x"}, format="json", **H)
    assert r.status_code == 409 and r.data["detail"] == "self_disable_requires_confirm"


# --- Rôle de membership -----------------------------------------------------

@pytest.fixture
def ws2(db):
    ws = Workspace.objects.create(name="P1-WS")
    uoa = User.objects.create_user("p1_oa", "oa@t.local", "x")
    uob = User.objects.create_user("p1_ob", "ob@t.local", "x")
    oa = Membership.objects.create(workspace=ws, user=uoa, role="owner")
    ob = Membership.objects.create(workspace=ws, user=uob, role="owner")
    return {"ws": ws, "oa": oa, "ob": ob}


def test_promotion_to_owner_via_patch_refused(su, ws2):
    um = User.objects.create_user("p1_m", "m@t.local", "x")
    m = Membership.objects.create(workspace=ws2["ws"], user=um, role="member", owner_scope=ws2["oa"])
    r = _cli(su).patch(f"/api/v1/platform/workspaces/{ws2['ws'].id}/memberships/{m.pk}/", {"role": "owner"}, format="json", **H)
    assert r.status_code == 422 and r.data["detail"] == "owner_promotion_requires_workflow"


def test_role_change_revalidates_d1_grants(su, ws2):
    ws = ws2["ws"]
    um = User.objects.create_user("p1_m2", "m2@t.local", "x")
    m = Membership.objects.create(workspace=ws, user=um, role="admin", owner_scope=ws2["oa"])
    rsa = RagSpace.objects.create(workspace=ws, name="RSA", owner_scope=ws2["oa"])
    RagSpaceAccess.objects.create(rag_space=rsa, user=um, space_role="manager")  # manager interdit pour viewer (D1)
    url = f"/api/v1/platform/workspaces/{ws.id}/memberships/{m.pk}/"
    r = _cli(su).patch(url, {"role": "viewer"}, format="json", **H)
    assert r.status_code == 409 and r.data["code"] == "grants_exceed_role"
    r2 = _cli(su).patch(url, {"role": "viewer", "revoke_incompatible": True}, format="json", **H)
    assert r2.status_code == 200 and r2.data["code"] == "role_changed"
    assert RagSpaceAccess.objects.filter(rag_space=rsa, user=um, revoked_at__isnull=False).exists()


def test_demote_last_owner_refused(su, db):
    ws = Workspace.objects.create(name="P1-SOLO")
    only = Membership.objects.create(workspace=ws, user=User.objects.create_user("p1_solo", "solo@t.local", "x"), role="owner")
    r = _cli(su).patch(f"/api/v1/platform/workspaces/{ws.id}/memberships/{only.pk}/", {"role": "member"}, format="json", **H)
    assert r.status_code == 409 and r.data["detail"] == "last_owner"


# --- Retrait de membership --------------------------------------------------

def test_remove_member_ok(su, ws2):
    um = User.objects.create_user("p1_rm", "rm@t.local", "x")
    m = Membership.objects.create(workspace=ws2["ws"], user=um, role="member", owner_scope=ws2["oa"])
    r = _cli(su).delete(f"/api/v1/platform/workspaces/{ws2['ws'].id}/memberships/{m.pk}/", **H)
    assert r.status_code == 200 and r.data["code"] == "membership_removed"
    assert not Membership.objects.filter(pk=m.pk).exists()


def test_remove_last_owner_refused(su, db):
    ws = Workspace.objects.create(name="P1-SOLO2")
    only = Membership.objects.create(workspace=ws, user=User.objects.create_user("p1_solo2", "s2@t.local", "x"), role="owner")
    r = _cli(su).delete(f"/api/v1/platform/workspaces/{ws.id}/memberships/{only.pk}/", **H)
    assert r.status_code == 409 and r.data["detail"] == "last_owner"


def test_remove_owner_with_nonempty_perimeter_refused(su, ws2):
    # oa a un membership rattaché → périmètre non vide.
    Membership.objects.create(workspace=ws2["ws"], user=User.objects.create_user("p1_scoped", "sc@t.local", "x"),
                              role="member", owner_scope=ws2["oa"])
    r = _cli(su).delete(f"/api/v1/platform/workspaces/{ws2['ws'].id}/memberships/{ws2['oa'].pk}/", **H)
    assert r.status_code == 409 and r.data["detail"] == "owner_scope_not_empty"


def test_remove_member_with_active_grants_blocks_then_revokes(su, ws2):
    ws = ws2["ws"]
    um = User.objects.create_user("p1_g", "g@t.local", "x")
    m = Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=ws2["oa"])
    rsa = RagSpace.objects.create(workspace=ws, name="RSG", owner_scope=ws2["oa"])
    RagSpaceAccess.objects.create(rag_space=rsa, user=um, space_role="editor")
    url = f"/api/v1/platform/workspaces/{ws.id}/memberships/{m.pk}/"
    r = _cli(su).delete(url, **H)
    assert r.status_code == 409 and r.data["code"] == "active_grants"
    r2 = _cli(su).delete(url, {"revoke_grants": True}, format="json", **H)
    assert r2.status_code == 200 and r2.data["code"] == "membership_removed"


# --- P2 — réactivation d'un workspace sans owner refusée --------------------

def test_enable_workspace_without_owner_refused(su, db):
    ws = Workspace.objects.create(name="P1-ORPHAN", is_active=False)
    r = _cli(su).post(f"/api/v1/platform/workspaces/{ws.id}/enable/", {"reason": "x"}, format="json", **H)
    assert r.status_code == 409 and r.data["detail"] == "workspace_without_owner"
