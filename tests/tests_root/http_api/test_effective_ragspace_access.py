"""Tests de l'enrichissement read-only pour l'UI owner-scope :
effective_ragspace_access (rôle effectif), serialize liste, endpoints access/eligible."""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services.access import effective_ragspace_access as eff

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="WS-EFF")
    ua = User.objects.create_user("eff_oa", "eff_oa@t.local", "x")
    ub = User.objects.create_user("eff_ob", "eff_ob@t.local", "x")
    uad = User.objects.create_user("eff_ad", "eff_ad@t.local", "x")
    um = User.objects.create_user("eff_ma", "eff_ma@t.local", "x")
    uv = User.objects.create_user("eff_va", "eff_va@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
    Membership.objects.create(workspace=ws, user=uad, role="admin", owner_scope=owner_a)
    Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner_a)
    Membership.objects.create(workspace=ws, user=uv, role="viewer", owner_scope=owner_a)
    rs_a = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner_a)
    rs_b = RagSpace.objects.create(workspace=ws, name="B", owner_scope=owner_b)
    return dict(ws=ws, ua=ua, ub=ub, uad=uad, um=um, uv=uv, owner_a=owner_a, rs_a=rs_a, rs_b=rs_b)


def _lvl(world, user, rs=None):
    return eff(user=user, rag_space=rs or world["rs_a"])["permission_level"]


# --- flags OFF (legacy) : rôle workspace ------------------------------------

def test_flags_off_legacy_levels(world, settings):
    settings.OWNER_SCOPE_ENABLED = False; settings.OWNER_SCOPE_AUDIT_ONLY = False
    assert _lvl(world, world["ua"]) == "manage"    # owner
    assert _lvl(world, world["uad"]) == "manage"   # admin
    assert _lvl(world, world["um"]) == "write"     # member
    assert _lvl(world, world["uv"]) == "read"      # viewer
    assert eff(user=world["ua"], rag_space=world["rs_a"])["is_owner_implicit"] is True


# --- owner_scope enforced (ACL off) -----------------------------------------

@pytest.fixture
def enforced(settings):
    settings.OWNER_SCOPE_ENABLED = True; settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False; settings.RAGSPACE_ACL_AUDIT_ONLY = True


def test_owner_scope_levels(world, enforced):
    assert _lvl(world, world["ua"]) == "manage"          # owner du périmètre
    assert eff(user=world["ua"], rag_space=world["rs_a"])["is_owner_implicit"] is True
    assert _lvl(world, world["uad"]) == "manage"         # admin du périmètre
    assert _lvl(world, world["um"]) == "write"           # member
    assert _lvl(world, world["uv"]) == "read"            # viewer
    # cross-périmètre → aucun accès
    assert _lvl(world, world["ua"], world["rs_b"]) is None


# --- ACL enforced : rôle d'espace explicite ---------------------------------

@pytest.fixture
def acl(settings):
    settings.OWNER_SCOPE_ENABLED = True; settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = True; settings.RAGSPACE_ACL_AUDIT_ONLY = False


def test_acl_levels(world, acl):
    assert _lvl(world, world["ua"]) == "manage"          # owner implicite
    assert _lvl(world, world["um"]) is None              # member sans grant → aucun accès
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["uv"], space_role="viewer")
    assert _lvl(world, world["uv"]) == "read"
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["um"], space_role="editor")
    assert _lvl(world, world["um"]) == "write"
    g = RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["uad"], space_role="manager")
    assert eff(user=world["uad"], rag_space=world["rs_a"])["permission_level"] == "manage"
    assert eff(user=world["uad"], rag_space=world["rs_a"])["space_role"] == "manager"


# --- HTTP : list enrichie + access user obj + eligible ----------------------

def _cli(u):
    c = APIClient(); c.force_authenticate(user=u); return c


def test_list_carries_effective_role(world, enforced):
    r = _cli(world["ua"]).get(f"/api/v1/rag-spaces/?workspace_id={world['ws'].id}")
    assert r.status_code == 200
    a = next(x for x in r.data if x["id"] == str(world["rs_a"].id))
    assert a["permission_level"] == "manage" and a["is_owner_implicit"] is True
    # owner_a ne voit pas rs_b (filtré par périmètre)
    assert all(x["id"] != str(world["rs_b"].id) for x in r.data)


def test_access_list_includes_user_object(world, enforced):
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["um"], space_role="editor")
    r = _cli(world["ua"]).get(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/")
    assert r.status_code == 200 and len(r.data) == 1
    assert r.data[0]["user"]["id"] == world["um"].id
    assert r.data[0]["user"]["email"] == "eff_ma@t.local"


def test_eligible_excludes_owner_cross_and_granted(world, enforced):
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["um"], space_role="editor")
    r = _cli(world["ua"]).get(f"/api/v1/rag-spaces/{world['rs_a'].id}/access/eligible/")
    assert r.status_code == 200
    ids = {row["user"]["id"] for row in r.data}
    assert world["uv"].id in ids           # viewer du périmètre, sans grant
    assert world["um"].id not in ids       # déjà un grant actif
    assert world["ua"].id not in ids       # owner (implicite)
    assert world["ub"].id not in ids       # owner du périmètre B
    # plafonds D1 corrects
    viewer_row = next(row for row in r.data if row["user"]["id"] == world["uv"].id)
    assert viewer_row["allowed_space_roles"] == ["viewer"]
