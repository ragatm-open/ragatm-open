"""Tests — complément Lot 6 : assign_workspace_owner + disable_workspace.

Couvre : création du premier owner, idempotence, refus workspace-déjà-owner /
membre-existant, promotion explicite, désactivation soft, et l'exclusion
documentée d'un workspace désactivé par le diagnostic.
"""
import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import owner_scope_migration as osm

User = get_user_model()


@pytest.fixture
def orphan(db):
    """Workspace sans owner, 1 RagSpace, + un user technique."""
    ws = Workspace.objects.create(name="ORPHAN-BENCH")
    rs = RagSpace.objects.create(workspace=ws, name="B1")
    tech = User.objects.create_user("ragatm-benchmark-owner", "bench@t.local", "x")
    return dict(ws=ws, rs=rs, tech=tech)


# --- create_workspace_owner --------------------------------------------------

def test_create_owner_dry_run_no_write(orphan):
    r = osm.create_workspace_owner(workspace=orphan["ws"], user=orphan["tech"], apply=False)
    assert r["code"] == osm.OWNER_CREATED and r["membership_id"] is None
    assert not Membership.objects.filter(workspace=orphan["ws"]).exists()


def test_create_owner_apply_then_idempotent(orphan):
    r1 = osm.create_workspace_owner(workspace=orphan["ws"], user=orphan["tech"], apply=True)
    assert r1["code"] == osm.OWNER_CREATED
    m = Membership.objects.get(workspace=orphan["ws"], user=orphan["tech"])
    assert m.role == "owner" and m.owner_scope_id is None
    r2 = osm.create_workspace_owner(workspace=orphan["ws"], user=orphan["tech"], apply=True)
    assert r2["code"] == osm.ALREADY_OWNER


def test_create_owner_refused_if_workspace_has_owner(orphan):
    osm.create_workspace_owner(workspace=orphan["ws"], user=orphan["tech"], apply=True)
    other = User.objects.create_user("u2", "u2@t.local", "x")
    r = osm.create_workspace_owner(workspace=orphan["ws"], user=other, apply=True)
    assert r["code"] == osm.WORKSPACE_HAS_OWNER
    # allow_additional lève le refus
    r2 = osm.create_workspace_owner(workspace=orphan["ws"], user=other, apply=True, allow_additional=True)
    assert r2["code"] == osm.OWNER_CREATED


def test_create_owner_member_exists_requires_promote(orphan):
    u = User.objects.create_user("m1", "m1@t.local", "x")
    Membership.objects.create(workspace=orphan["ws"], user=u, role="member")
    r = osm.create_workspace_owner(workspace=orphan["ws"], user=u, apply=True)
    assert r["code"] == osm.MEMBER_EXISTS
    r2 = osm.create_workspace_owner(workspace=orphan["ws"], user=u, apply=True, promote=True)
    assert r2["code"] == osm.PROMOTED
    u_m = Membership.objects.get(workspace=orphan["ws"], user=u)
    assert u_m.role == "owner"


def test_owner_multiple_workspaces_independent(orphan):
    ws2 = Workspace.objects.create(name="ORPHAN-2")
    osm.create_workspace_owner(workspace=orphan["ws"], user=orphan["tech"], apply=True)
    osm.create_workspace_owner(workspace=ws2, user=orphan["tech"], apply=True)
    assert Membership.objects.filter(user=orphan["tech"], role="owner").count() == 2


# --- set_workspace_active ----------------------------------------------------

def test_disable_and_idempotent(db):
    ws = Workspace.objects.create(name="EMPTY")
    r = osm.set_workspace_active(workspace=ws, active=False, apply=True)
    assert r["code"] == osm.WORKSPACE_DISABLED
    ws.refresh_from_db(); assert ws.is_active is False
    r2 = osm.set_workspace_active(workspace=ws, active=False, apply=True)
    assert r2["code"] == osm.ALREADY_DISABLED


# --- Full orphan flow via commands + diagnostic exclusion --------------------

def test_orphan_flow_then_diagnostic_ok(orphan):
    # 1) owner technique
    call_command("assign_workspace_owner", "--workspace-id", str(orphan["ws"].id),
                 "--user-id", str(orphan["tech"].id), "--apply", stdout=io.StringIO())
    # 2) backfill du ragspace
    call_command("backfill_owner_scopes", "--workspace-id", str(orphan["ws"].id),
                 "--apply", stdout=io.StringIO())
    orphan["rs"].refresh_from_db()
    owner_m = Membership.objects.get(workspace=orphan["ws"], role="owner")
    assert orphan["rs"].owner_scope_id == owner_m.pk
    # 3) diagnostic strict de ce workspace → aucune anomalie
    out = io.StringIO()
    call_command("audit_workspace_owner_scopes", "--workspace-id", str(orphan["ws"].id),
                 "--format", "json", stdout=out)
    data = json.loads(out.getvalue())
    assert data["workspaces"][0]["anomalies"] == []


def test_disabled_workspace_excluded_from_blocking(db):
    ws = Workspace.objects.create(name="EMPTY", is_active=False)
    out = io.StringIO()
    call_command("audit_workspace_owner_scopes", "--workspace-id", str(ws.id),
                 "--format", "json", stdout=out)
    entry = json.loads(out.getvalue())["workspaces"][0]
    assert entry["is_active"] is False
    assert [a["code"] for a in entry["anomalies"]] == ["workspace_disabled"]
    assert entry["has_integrity_violation"] is False


def test_assign_workspace_owner_conflict_exit_1(orphan):
    osm.create_workspace_owner(workspace=orphan["ws"], user=orphan["tech"], apply=True)
    other = User.objects.create_user("u3", "u3@t.local", "x")
    with pytest.raises(SystemExit) as exc:
        call_command("assign_workspace_owner", "--workspace-id", str(orphan["ws"].id),
                     "--user-id", str(other.id), "--apply", stdout=io.StringIO())
    assert exc.value.code == 1
