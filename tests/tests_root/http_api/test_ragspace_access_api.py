"""Tests — endpoints platform RagSpaceAccess (list/grant/change/revoke, manage)."""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="WS-ACLAPI")
    ua = User.objects.create_user("api_oa", "api_oa@t.local", "x")
    ub = User.objects.create_user("api_ob", "api_ob@t.local", "x")
    uad = User.objects.create_user("api_ad", "api_ad@t.local", "x")
    um = User.objects.create_user("api_ma", "api_ma@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
    Membership.objects.create(workspace=ws, user=uad, role="admin", owner_scope=owner_a)
    Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner_a)
    rs_a = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner_a)
    rs_b = RagSpace.objects.create(workspace=ws, name="B", owner_scope=owner_b)
    return dict(ws=ws, ua=ua, ub=ub, uad=uad, um=um, owner_a=owner_a, rs_a=rs_a, rs_b=rs_b)


def client(user):
    c = APIClient(); c.force_authenticate(user=user); return c


def test_owner_grants_and_lists(world):
    c = client(world["ua"])
    r = c.post(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/",
               {"user_id": world["um"].id, "space_role": "editor"}, format="json")
    assert r.status_code == 201 and r.data["code"] == "granted"
    lst = c.get(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/")
    assert lst.status_code == 200 and len(lst.data) == 1 and lst.data[0]["space_role"] == "editor"


def test_admin_of_perimeter_can_manage(world):
    r = client(world["uad"]).post(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/",
                                  {"user_id": world["um"].id, "space_role": "viewer"}, format="json")
    assert r.status_code == 201


def test_cross_perimeter_actor_gets_404(world):
    # owner_b gouverne le périmètre B, pas rs_a (périmètre A) -> 404 anti-énumération
    r = client(world["ub"]).get(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/")
    assert r.status_code == 404


def test_member_cannot_manage(world):
    r = client(world["um"]).post(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/",
                                 {"user_id": world["um"].id, "space_role": "viewer"}, format="json")
    assert r.status_code == 404


def test_d1_cap_enforced_over_api(world):
    r = client(world["ua"]).post(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/",
                                 {"user_id": world["um"].id, "space_role": "manager"}, format="json")
    assert r.status_code == 400 and r.data["code"] == "invalid"


def test_cross_owner_scope_grantee_rejected(world):
    # accorder à owner_b (périmètre B) un accès sur rs_a (périmètre A) -> invalide
    r = client(world["ua"]).post(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/",
                                 {"user_id": world["ub"].id, "space_role": "viewer"}, format="json")
    assert r.status_code == 400


def test_change_role_then_revoke_keeps_history(world):
    c = client(world["ua"])
    g = c.post(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/",
               {"user_id": world["um"].id, "space_role": "viewer"}, format="json").data
    ch = c.patch(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/{g['grant_id']}/",
                 {"space_role": "editor"}, format="json")
    assert ch.status_code == 200 and ch.data["code"] == "role_changed"
    new_id = ch.data["grant_id"]
    dl = c.delete(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/{new_id}/")
    assert dl.status_code == 200 and dl.data["code"] == "revoked"
    # plus d'accès actif, mais l'historique (2 lignes) est conservé
    assert RagSpaceAccess.objects.filter(rag_space=world["rs_a"], user=world["um"], revoked_at__isnull=True).count() == 0
    assert RagSpaceAccess.objects.filter(rag_space=world["rs_a"], user=world["um"]).count() == 2


def test_grant_on_deleted_ragspace_404(world):
    from django.utils import timezone
    world["rs_a"].deleted_at = timezone.now(); world["rs_a"].save(update_fields=["deleted_at"])
    r = client(world["ua"]).post(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/",
                                 {"user_id": world["um"].id, "space_role": "viewer"}, format="json")
    assert r.status_code == 404
