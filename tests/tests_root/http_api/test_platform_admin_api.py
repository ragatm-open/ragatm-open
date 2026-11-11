"""Tests de la console d'administration PLATEFORME (superuser only).

Couvre : matrice d'autorisation, lecture (sans contenu), création workspace+owner,
memberships + owner-scope, désactivation douce, transfert (preview/apply NO-GO),
et l'absence de bypass de contenu pour un superuser.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db
H = {"HTTP_HOST": "localhost"}


def _cli(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user=user)
    return c


@pytest.fixture
def su(db):
    return User.objects.create_user("plat_su", "plat_su@t.local", "x", is_superuser=True, is_staff=True)


@pytest.fixture
def normal(db):
    return User.objects.create_user("plat_n", "plat_n@t.local", "x")


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="PLAT-WS")
    uo = User.objects.create_user("plat_o", "plat_o@t.local", "x")
    owner = Membership.objects.create(workspace=ws, user=uo, role="owner")
    return {"ws": ws, "owner_user": uo, "owner": owner}


# --- Autorisation -----------------------------------------------------------

def test_auth_matrix(su, normal):
    assert _cli().get("/api/v1/platform/users/", **H).status_code == 401
    assert _cli(normal).get("/api/v1/platform/users/", **H).status_code == 403
    assert _cli(su).get("/api/v1/platform/users/", **H).status_code == 200


def test_workspace_owner_non_superuser_is_forbidden(world):
    # Un owner de workspace (non superuser) n'a PAS accès à la console plateforme.
    assert _cli(world["owner_user"]).get("/api/v1/platform/workspaces/", **H).status_code == 403


def test_inactive_superuser_forbidden(su):
    su.is_active = False
    su.save(update_fields=["is_active"])
    assert _cli(su).get("/api/v1/platform/users/", **H).status_code == 403


# --- Lecture (sans contenu) -------------------------------------------------

def test_users_list_has_no_secret(su):
    r = _cli(su).get("/api/v1/platform/users/", **H)
    assert r.status_code == 200 and r.data["total"] >= 1
    for item in r.data["items"]:
        assert "password" not in item and "token" not in item
        assert set(["id", "email", "memberships_count", "owned_workspaces_count"]).issubset(item.keys())


def test_workspace_detail_exposes_no_document_content(su, world):
    rs = RagSpace.objects.create(workspace=world["ws"], name="RS", owner_scope=world["owner"])
    r = _cli(su).get(f"/api/v1/platform/workspaces/{world['ws'].id}/", **H)
    assert r.status_code == 200
    space = r.data["ragspaces"][0]
    assert set(space.keys()) == {"id", "name", "is_active", "deleted_at", "owner_scope_id", "created_by_id", "documents_count"}
    # aucune clé de contenu (documents/chunks/prompts/chat)
    assert "documents" not in space and "chunks" not in space


def test_diagnostics_consistent_with_command(su, world):
    from django.core.management import call_command
    import io, json
    buf = io.StringIO()
    call_command("audit_workspace_owner_scopes", "--json", stdout=buf)
    cli = json.loads(buf.getvalue())["workspaces"]
    api = _cli(su).get("/api/v1/platform/owner-scope-diagnostics/", **H).data["workspaces"]
    assert cli == api


def test_dashboard(su, world):
    r = _cli(su).get("/api/v1/platform/dashboard/", **H)
    assert r.status_code == 200
    assert r.data["workspaces_total"] >= 1 and "integrity_violations" in r.data


# --- Création workspace + owner ---------------------------------------------

def test_create_workspace_with_owner(su):
    u = User.objects.create_user("plat_new_o", "plat_new_o@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/", {"name": "NewWS", "owner_user_id": u.id}, format="json", **H)
    assert r.status_code == 201 and r.data["code"] == "workspace_created"
    m = Membership.objects.get(pk=r.data["owner"]["membership_id"])
    assert m.role == "owner" and m.owner_scope_id is None


def test_create_workspace_rejects_inactive_owner(su):
    u = User.objects.create_user("plat_inact", "plat_inact@t.local", "x", is_active=False)
    before = Workspace.objects.count()
    r = _cli(su).post("/api/v1/platform/workspaces/", {"name": "X", "owner_user_id": u.id}, format="json", **H)
    assert r.status_code == 400 and r.data["detail"] == "user_inactive"
    assert Workspace.objects.count() == before  # aucun workspace créé


def test_assign_owner_idempotent_and_conflict(su, world):
    r1 = _cli(su).post(f"/api/v1/platform/workspaces/{world['ws'].id}/owner/", {"user_id": world["owner_user"].id}, format="json", **H)
    assert r1.status_code == 200 and r1.data["code"] == "already_owner"
    other = User.objects.create_user("plat_x", "plat_x@t.local", "x")
    r2 = _cli(su).post(f"/api/v1/platform/workspaces/{world['ws'].id}/owner/", {"user_id": other.id}, format="json", **H)
    assert r2.status_code == 409 and r2.data["code"] == "workspace_has_owner"


# --- Memberships + owner-scope ----------------------------------------------

def test_add_member_requires_scope(su, world):
    u = User.objects.create_user("plat_m", "plat_m@t.local", "x")
    r = _cli(su).post(f"/api/v1/platform/workspaces/{world['ws'].id}/memberships/", {"user_id": u.id, "role": "member"}, format="json", **H)
    assert r.status_code == 422 and r.data["detail"] == "owner_scope_required"


def test_add_member_rejects_cross_workspace_owner(su, world):
    other_ws = Workspace.objects.create(name="OTHER")
    ou = User.objects.create_user("plat_oo", "plat_oo@t.local", "x")
    other_owner = Membership.objects.create(workspace=other_ws, user=ou, role="owner")
    u = User.objects.create_user("plat_m2", "plat_m2@t.local", "x")
    r = _cli(su).post(f"/api/v1/platform/workspaces/{world['ws'].id}/memberships/",
                      {"user_id": u.id, "role": "member", "owner_membership_id": other_owner.pk}, format="json", **H)
    assert r.status_code == 422 and r.data["detail"] == "cross_workspace"


def test_add_member_ok_and_duplicate(su, world):
    u = User.objects.create_user("plat_m3", "plat_m3@t.local", "x")
    body = {"user_id": u.id, "role": "member", "owner_membership_id": world["owner"].pk}
    r = _cli(su).post(f"/api/v1/platform/workspaces/{world['ws'].id}/memberships/", body, format="json", **H)
    assert r.status_code == 201 and r.data["code"] == "membership_created"
    m = Membership.objects.get(pk=r.data["membership_id"])
    assert m.owner_scope_id == world["owner"].pk
    r2 = _cli(su).post(f"/api/v1/platform/workspaces/{world['ws'].id}/memberships/", body, format="json", **H)
    assert r2.status_code == 200 and r2.data["code"] == "already_member"


def test_owner_scope_change_blocks_then_revokes_incompatible(su, world):
    ws = world["ws"]
    ob = Membership.objects.create(workspace=ws, user=User.objects.create_user("plat_ob", "ob@t.local", "x"), role="owner")
    um = User.objects.create_user("plat_um", "um@t.local", "x")
    mm = Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=world["owner"])
    rsa = RagSpace.objects.create(workspace=ws, name="RSA", owner_scope=world["owner"])
    RagSpaceAccess.objects.create(rag_space=rsa, user=um, space_role="editor")
    url = f"/api/v1/platform/workspaces/{ws.id}/memberships/{mm.pk}/owner-scope/"
    r = _cli(su).patch(url, {"owner_membership_id": ob.pk}, format="json", **H)
    assert r.status_code == 409 and r.data["code"] == "grants_incompatible"
    r2 = _cli(su).patch(url, {"owner_membership_id": ob.pk, "revoke_incompatible": True}, format="json", **H)
    assert r2.status_code == 200 and r2.data["code"] == "membership_owner_scope_changed"
    mm.refresh_from_db()
    assert mm.owner_scope_id == ob.pk
    assert RagSpaceAccess.objects.filter(rag_space=rsa, user=um, revoked_at__isnull=False).exists()


# --- Désactivation / réactivation -------------------------------------------

def test_disable_requires_reason_and_is_soft(su, world):
    ws = world["ws"]
    assert _cli(su).post(f"/api/v1/platform/workspaces/{ws.id}/disable/", {}, format="json", **H).status_code == 400
    r = _cli(su).post(f"/api/v1/platform/workspaces/{ws.id}/disable/", {"reason": "obsolete"}, format="json", **H)
    assert r.status_code == 200 and r.data["code"] == "workspace_disabled"
    ws.refresh_from_db()
    assert ws.is_active is False and Workspace.objects.filter(pk=ws.pk).exists()  # pas de hard-delete
    r2 = _cli(su).post(f"/api/v1/platform/workspaces/{ws.id}/disable/", {"reason": "again"}, format="json", **H)
    assert r2.data["code"] == "already_disabled"
    r3 = _cli(su).post(f"/api/v1/platform/workspaces/{ws.id}/enable/", {"reason": "restore"}, format="json", **H)
    assert r3.data["code"] == "workspace_enabled"


# --- Transfert (preview / apply NO-GO) --------------------------------------

def test_transfer_preview_is_read_only(su, world):
    ws = world["ws"]
    ob = Membership.objects.create(workspace=ws, user=User.objects.create_user("plat_tb", "tb@t.local", "x"), role="owner")
    mm = Membership.objects.create(workspace=ws, user=User.objects.create_user("plat_tm", "tm@t.local", "x"),
                                   role="member", owner_scope=world["owner"])
    r = _cli(su).post("/api/v1/platform/owner-scope-transfers/preview/",
                      {"workspace_id": ws.id, "source_owner_membership_id": world["owner"].pk,
                       "target_owner_membership_id": ob.pk}, format="json", **H)
    assert r.status_code == 200 and r.data["apply_supported"] is False
    assert r.data["affected_memberships"][0]["membership_id"] == mm.pk
    mm.refresh_from_db()
    assert mm.owner_scope_id == world["owner"].pk  # aucune écriture


def test_transfer_apply_is_blocked(su, world):
    r = _cli(su).post("/api/v1/platform/owner-scope-transfers/apply/", {"workspace_id": world["ws"].id}, format="json", **H)
    assert r.status_code == 501 and r.data["detail"] == "transfer_apply_not_implemented"
