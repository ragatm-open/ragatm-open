"""Tests — commandes/serveur de backfill owner_scope (Lot 6).

Couvre : dry-run vs apply, idempotence, backfill mono-owner déterministe, refus
auto multi-owner / sans-owner, refus cross-workspace & cible non-owner, conflit
non écrasé, et codes de sortie des commandes.
"""
import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import owner_scope_migration as osm

User = get_user_model()


@pytest.fixture
def mono(db):
    """Workspace mono-owner : 1 owner, 1 member, 1 viewer, 2 RagSpaces (scopes NULL)."""
    ws = Workspace.objects.create(name="MONO")
    uo = User.objects.create_user("o", "o@t.local", "x")
    um = User.objects.create_user("m", "m@t.local", "x")
    uv = User.objects.create_user("v", "v@t.local", "x")
    owner = Membership.objects.create(workspace=ws, user=uo, role="owner")
    member = Membership.objects.create(workspace=ws, user=um, role="member")
    viewer = Membership.objects.create(workspace=ws, user=uv, role="viewer")
    rs1 = RagSpace.objects.create(workspace=ws, name="S1")
    rs2 = RagSpace.objects.create(workspace=ws, name="S2")
    return dict(ws=ws, owner=owner, member=member, viewer=viewer, rs1=rs1, rs2=rs2)


# --- assign_membership_scope -------------------------------------------------

def test_dry_run_writes_nothing(mono):
    r = osm.assign_membership_scope(membership=mono["member"], owner=mono["owner"], apply=False)
    assert r["code"] == osm.ASSIGNED and r["after"] == mono["owner"].pk
    mono["member"].refresh_from_db()
    assert mono["member"].owner_scope_id is None  # rien écrit


def test_apply_then_idempotent(mono):
    r1 = osm.assign_membership_scope(membership=mono["member"], owner=mono["owner"], apply=True)
    assert r1["code"] == osm.ASSIGNED
    mono["member"].refresh_from_db()
    assert mono["member"].owner_scope_id == mono["owner"].pk
    # ré-appliquer → idempotent
    r2 = osm.assign_membership_scope(membership=mono["member"], owner=mono["owner"], apply=True)
    assert r2["code"] == osm.ALREADY_SCOPED


def test_owner_membership_skipped(mono):
    r = osm.assign_membership_scope(membership=mono["owner"], owner=mono["owner"], apply=True)
    assert r["code"] == osm.SKIPPED_OWNER


def test_conflict_not_overwritten(mono):
    # rattache member à owner, puis tente un autre owner → conflit, pas d'écrasement
    osm.assign_membership_scope(membership=mono["member"], owner=mono["owner"], apply=True)
    u2 = User.objects.create_user("o2", "o2@t.local", "x")
    owner2 = Membership.objects.create(workspace=mono["ws"], user=u2, role="owner")
    r = osm.assign_membership_scope(membership=mono["member"], owner=owner2, apply=True)
    assert r["code"] == osm.CONFLICT
    mono["member"].refresh_from_db()
    assert mono["member"].owner_scope_id == mono["owner"].pk  # inchangé


def test_reject_cross_workspace_owner(mono):
    other = Workspace.objects.create(name="OTHER")
    foreign_owner = Membership.objects.create(workspace=other, user=mono["owner"].user, role="owner")
    r = osm.assign_membership_scope(membership=mono["member"], owner=foreign_owner, apply=True)
    assert r["code"] == osm.INVALID_CROSS_WORKSPACE


def test_reject_non_owner_target(mono):
    r = osm.assign_membership_scope(membership=mono["member"], owner=mono["viewer"], apply=True)
    assert r["code"] == osm.INVALID_TARGET_NOT_OWNER


# --- assign_ragspace_scope ---------------------------------------------------

def test_ragspace_assign_and_idempotent(mono):
    r = osm.assign_ragspace_scope(rag_space=mono["rs1"], owner=mono["owner"], apply=True)
    assert r["code"] == osm.ASSIGNED
    mono["rs1"].refresh_from_db()
    assert mono["rs1"].owner_scope_id == mono["owner"].pk
    r2 = osm.assign_ragspace_scope(rag_space=mono["rs1"], owner=mono["owner"], apply=True)
    assert r2["code"] == osm.ALREADY_SCOPED


def test_ragspace_reject_cross_workspace(mono):
    other = Workspace.objects.create(name="OTHER")
    foreign_owner = Membership.objects.create(workspace=other, user=mono["owner"].user, role="owner")
    r = osm.assign_ragspace_scope(rag_space=mono["rs1"], owner=foreign_owner, apply=True)
    assert r["code"] == osm.INVALID_CROSS_WORKSPACE


# --- backfill_workspace ------------------------------------------------------

def test_backfill_mono_owner_deterministic(mono):
    res = osm.backfill_workspace(workspace=mono["ws"], apply=True)
    assert res["code"] == "ok" and res["owner_membership_id"] == mono["owner"].pk
    counts = osm.summarize(res["changes"])
    assert counts.get(osm.ASSIGNED) == 4  # member + viewer + 2 ragspaces
    mono["member"].refresh_from_db(); mono["viewer"].refresh_from_db()
    mono["rs1"].refresh_from_db(); mono["rs2"].refresh_from_db()
    assert mono["member"].owner_scope_id == mono["owner"].pk
    assert mono["viewer"].owner_scope_id == mono["owner"].pk
    assert mono["rs1"].owner_scope_id == mono["owner"].pk
    assert mono["rs2"].owner_scope_id == mono["owner"].pk


def test_backfill_dry_run_no_write(mono):
    res = osm.backfill_workspace(workspace=mono["ws"], apply=False)
    assert osm.summarize(res["changes"]).get(osm.ASSIGNED) == 4
    mono["member"].refresh_from_db()
    assert mono["member"].owner_scope_id is None


def test_backfill_multi_owner_refused(mono):
    u2 = User.objects.create_user("o2", "o2@t.local", "x")
    Membership.objects.create(workspace=mono["ws"], user=u2, role="owner")
    res = osm.backfill_workspace(workspace=mono["ws"], apply=True)
    assert res["code"] == osm.NO_SINGLE_OWNER and res["changes"] == []
    mono["member"].refresh_from_db()
    assert mono["member"].owner_scope_id is None  # aucune écriture


def test_backfill_no_owner_refused(db):
    ws = Workspace.objects.create(name="ORPHAN")
    u = User.objects.create_user("x1", "x1@t.local", "x")
    Membership.objects.create(workspace=ws, user=u, role="member")
    res = osm.backfill_workspace(workspace=ws, apply=True)
    assert res["code"] == osm.NO_SINGLE_OWNER


# --- Commandes (exit codes) --------------------------------------------------

def test_command_backfill_apply(mono):
    out = io.StringIO()
    call_command("backfill_owner_scopes", "--workspace-id", str(mono["ws"].id), "--apply", stdout=out)
    mono["rs1"].refresh_from_db()
    assert mono["rs1"].owner_scope_id == mono["owner"].pk


def test_command_assign_membership_conflict_exit_1(mono):
    osm.assign_membership_scope(membership=mono["member"], owner=mono["owner"], apply=True)
    u2 = User.objects.create_user("o2", "o2@t.local", "x")
    owner2 = Membership.objects.create(workspace=mono["ws"], user=u2, role="owner")
    with pytest.raises(SystemExit) as exc:
        call_command(
            "assign_membership_owner_scope",
            "--membership-id", str(mono["member"].id),
            "--owner-membership-id", str(owner2.id),
            "--apply",
            stdout=io.StringIO(),
        )
    assert exc.value.code == 1


def test_command_backfill_requires_selection(db):
    from django.core.management.base import CommandError
    with pytest.raises(CommandError):
        call_command("backfill_owner_scopes", stdout=io.StringIO())
