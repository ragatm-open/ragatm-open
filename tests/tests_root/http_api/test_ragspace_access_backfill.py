"""Tests — service d'octroi RagSpaceAccess + commande de backfill (Lot ACL)."""
import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import ragspace_access as svc

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="WS-ACL")
    ua = User.objects.create_user("acl_oa", "acl_oa@t.local", "x")
    ub = User.objects.create_user("acl_ob", "acl_ob@t.local", "x")
    um = User.objects.create_user("acl_ma", "acl_ma@t.local", "x")
    uv = User.objects.create_user("acl_va", "acl_va@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
    member_a = Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner_a)
    viewer_a = Membership.objects.create(workspace=ws, user=uv, role="viewer", owner_scope=owner_a)
    rs_a = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner_a)
    rs_b = RagSpace.objects.create(workspace=ws, name="B", owner_scope=owner_b)
    return dict(ws=ws, ua=ua, ub=ub, um=um, uv=uv, owner_a=owner_a, owner_b=owner_b,
                member_a=member_a, viewer_a=viewer_a, rs_a=rs_a, rs_b=rs_b)


# --- Service : règles d'octroi ----------------------------------------------

def test_owner_is_implicit_no_grant(world):
    r = svc.grant_access(rag_space=world["rs_a"], user=world["ua"], space_role="editor",
                         assigned_by=None, apply=True)
    assert r["code"] == svc.OWNER_IMPLICIT
    assert not RagSpaceAccess.objects.filter(rag_space=world["rs_a"], user=world["ua"]).exists()


def test_grant_dry_run_then_apply_idempotent(world):
    r0 = svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="editor",
                          assigned_by=None, apply=False)
    assert r0["code"] == svc.GRANTED
    assert not RagSpaceAccess.objects.filter(rag_space=world["rs_a"], user=world["um"]).exists()
    r1 = svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="editor",
                          assigned_by=None, apply=True)
    assert r1["code"] == svc.GRANTED
    r2 = svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="editor",
                          assigned_by=None, apply=True)
    assert r2["code"] == svc.ALREADY_GRANTED


def test_d1_cap_member_cannot_manager(world):
    r = svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="manager",
                         assigned_by=None, apply=True)
    assert r["code"] == svc.INVALID and "D1" in r["detail"]


def test_d1_cap_viewer_only_viewer(world):
    assert svc.grant_access(rag_space=world["rs_a"], user=world["uv"], space_role="editor",
                            assigned_by=None, apply=True)["code"] == svc.INVALID
    assert svc.grant_access(rag_space=world["rs_a"], user=world["uv"], space_role="viewer",
                            assigned_by=None, apply=True)["code"] == svc.GRANTED


def test_cross_owner_scope_rejected(world):
    # member du périmètre A ne peut pas recevoir un accès sur un espace du périmètre B
    r = svc.grant_access(rag_space=world["rs_b"], user=world["um"], space_role="viewer",
                         assigned_by=None, apply=True)
    assert r["code"] == svc.INVALID


def test_ragspace_deleted_rejected(world):
    world["rs_a"].deleted_at = timezone.now(); world["rs_a"].save(update_fields=["deleted_at"])
    assert svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="editor",
                            assigned_by=None, apply=True)["code"] == svc.RAGSPACE_DELETED


def test_conflict_then_change_role_keeps_history(world):
    svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="viewer",
                     assigned_by=None, apply=True)
    # même user, rôle différent, sans allow_role_change => conflit
    c = svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="editor",
                         assigned_by=None, apply=True)
    assert c["code"] == svc.CONFLICT
    # change_role : révoque l'ancien + crée le nouveau ; historique conservé
    ch = svc.change_role(rag_space=world["rs_a"], user=world["um"], space_role="editor",
                         assigned_by=None, apply=True)
    assert ch["code"] == svc.ROLE_CHANGED
    active = RagSpaceAccess.objects.filter(rag_space=world["rs_a"], user=world["um"], revoked_at__isnull=True)
    assert active.count() == 1 and active.first().space_role == "editor"
    assert RagSpaceAccess.objects.filter(rag_space=world["rs_a"], user=world["um"]).count() == 2  # historique


def test_revoke_sets_revoked_at_not_delete(world):
    g = svc.grant_access(rag_space=world["rs_a"], user=world["um"], space_role="editor",
                         assigned_by=None, apply=True)
    r = svc.revoke_access(rag_space=world["rs_a"], user=world["um"], apply=True)
    assert r["code"] == svc.REVOKED
    row = RagSpaceAccess.objects.get(id=g["grant_id"])
    assert row.revoked_at is not None  # ligne conservée, non supprimée
    assert svc.list_grants(rag_space=world["rs_a"]).count() == 0  # plus d'actif
    assert svc.list_grants(rag_space=world["rs_a"], include_revoked=True).count() == 1


def test_revoke_not_found(world):
    assert svc.revoke_access(rag_space=world["rs_a"], user=world["um"], apply=True)["code"] == svc.GRANT_NOT_FOUND


# --- Commande backfill -------------------------------------------------------

def _mapping(tmp_path, ws_id, grants):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"workspace_id": ws_id, "grants": grants}))
    return str(p)


def test_backfill_dry_run_no_write(world, tmp_path):
    m = _mapping(tmp_path, world["ws"].id, [
        {"user_id": world["um"].id, "rag_space_id": str(world["rs_a"].id), "space_role": "editor"},
    ])
    out = io.StringIO()
    call_command("backfill_ragspace_access", "--workspace-id", str(world["ws"].id),
                 "--mapping-file", m, "--format", "json", stdout=out)
    assert json.loads(out.getvalue())["summary"].get("granted") == 1
    assert not RagSpaceAccess.objects.exists()  # dry-run


def test_backfill_apply_and_atomic_on_error(world, tmp_path):
    # 1 grant valide + 1 grant invalide (D1) => rollback total (atomicité)
    m = _mapping(tmp_path, world["ws"].id, [
        {"user_id": world["um"].id, "rag_space_id": str(world["rs_a"].id), "space_role": "editor"},
        {"user_id": world["uv"].id, "rag_space_id": str(world["rs_a"].id), "space_role": "manager"},  # D1 viol
    ])
    with pytest.raises(SystemExit) as exc:
        call_command("backfill_ragspace_access", "--workspace-id", str(world["ws"].id),
                     "--mapping-file", m, "--apply", stdout=io.StringIO())
    assert exc.value.code == 1
    assert not RagSpaceAccess.objects.exists()  # rien écrit : all-or-nothing


def test_backfill_apply_success(world, tmp_path):
    m = _mapping(tmp_path, world["ws"].id, [
        {"user_id": world["um"].id, "rag_space_id": str(world["rs_a"].id), "space_role": "editor"},
        {"user_id": world["uv"].id, "rag_space_id": str(world["rs_a"].id), "space_role": "viewer"},
    ])
    call_command("backfill_ragspace_access", "--workspace-id", str(world["ws"].id),
                 "--mapping-file", m, "--apply", stdout=io.StringIO())
    assert RagSpaceAccess.objects.filter(revoked_at__isnull=True).count() == 2


def test_backfill_rejects_wrong_workspace_in_file(world, tmp_path):
    from django.core.management.base import CommandError
    m = _mapping(tmp_path, 999, [])
    with pytest.raises(CommandError):
        call_command("backfill_ragspace_access", "--workspace-id", str(world["ws"].id),
                     "--mapping-file", m, stdout=io.StringIO())
