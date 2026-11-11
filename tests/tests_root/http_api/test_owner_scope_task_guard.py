"""Lot 3 — revalidation des tâches async à l'exécution + garde de cache.

Le cœur sécuritaire est le service `task_guard` : ces tests mutent l'état
(révocation, transfert de périmètre, désactivation workspace, soft-delete espace,
retrait de membership, rôle insuffisant) et vérifient la décision + la raison
stable. Plus : une tâche réelle (résumé) annulée à l'exécution, et l'invariant
« permission AVANT lecture (cache) ».
"""
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.rag_core.models import Document, RagSpace, RagSpaceAccess
from apps.rag_core.models.summary import DocumentSummary
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import task_guard as tg

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="WS-TG")
    ua = User.objects.create_user("tg_oa", "tg_oa@t.local", "x")
    ub = User.objects.create_user("tg_ob", "tg_ob@t.local", "x")
    um = User.objects.create_user("tg_ma", "tg_ma@t.local", "x")
    uv = User.objects.create_user("tg_va", "tg_va@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
    member_a = Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner_a)
    viewer_a = Membership.objects.create(workspace=ws, user=uv, role="viewer", owner_scope=owner_a)
    rs_a = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner_a)
    doc_a = Document.objects.create(rag_space=rs_a, filename="a.pdf", sha256="a" * 64)
    return dict(ws=ws, ua=ua, ub=ub, um=um, uv=uv, owner_a=owner_a, owner_b=owner_b,
                member_a=member_a, viewer_a=viewer_a, rs_a=rs_a, doc_a=doc_a)


@pytest.fixture
def enforced(settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False
    settings.RAGSPACE_ACL_AUDIT_ONLY = False
    return settings


def _rev(world, user, perm="read", **kw):
    return tg.revalidate_task_access(user_id=user.id, rag_space_id=world["rs_a"].id,
                                     required_permission=perm, **kw)


# --- Décision du guard (matrice mandatée) -----------------------------------

def test_task_executes_when_access_unchanged(world, enforced):
    assert _rev(world, world["um"]).ok is True


def test_task_cancelled_after_membership_removal(world, enforced):
    world["member_a"].delete()
    r = _rev(world, world["um"])
    assert not r.ok and r.reason == tg.MEMBERSHIP_MISSING


def test_task_cancelled_after_workspace_disable(world, enforced):
    world["ws"].is_active = False; world["ws"].save(update_fields=["is_active"])
    r = _rev(world, world["um"])
    assert not r.ok and r.reason == tg.WORKSPACE_DISABLED


def test_task_cancelled_after_ragspace_soft_delete(world, enforced):
    world["rs_a"].deleted_at = timezone.now(); world["rs_a"].save(update_fields=["deleted_at"])
    r = _rev(world, world["um"])
    assert not r.ok and r.reason == tg.RAGSPACE_DELETED


def test_task_cancelled_after_owner_scope_transfer(world, enforced):
    # le RagSpace est transféré au périmètre B : l'acteur (scope A) ne correspond plus
    world["rs_a"].owner_scope = world["owner_b"]; world["rs_a"].save(update_fields=["owner_scope"])
    r = _rev(world, world["um"])
    assert not r.ok and r.reason == tg.OWNER_SCOPE_MISMATCH
    # avec snapshot d'enqueue, on distingue le transfert
    r2 = _rev(world, world["um"], enqueue_owner_scope_id=world["owner_a"].id)
    assert not r2.ok and r2.reason == tg.OWNER_SCOPE_TRANSFERRED


def test_task_cancelled_after_access_revocation(world, settings):
    settings.OWNER_SCOPE_ENABLED = True; settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = True; settings.RAGSPACE_ACL_AUDIT_ONLY = False
    acc = RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["um"], space_role="editor")
    assert _rev(world, world["um"], perm="write").ok is True
    acc.revoked_at = timezone.now(); acc.save(update_fields=["revoked_at"])
    r = _rev(world, world["um"], perm="write")
    assert not r.ok and r.reason == tg.ACCESS_REVOKED


def test_viewer_write_task_rejected_at_execution(world, settings):
    settings.OWNER_SCOPE_ENABLED = True; settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = True; settings.RAGSPACE_ACL_AUDIT_ONLY = False
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["uv"], space_role="viewer")
    r = tg.revalidate_task_access(user_id=world["uv"].id, rag_space_id=world["rs_a"].id, required_permission="write")
    assert not r.ok and r.reason == tg.INSUFFICIENT_SPACE_ROLE


def test_actor_inactive(world, enforced):
    world["um"].is_active = False; world["um"].save(update_fields=["is_active"])
    r = _rev(world, world["um"])
    assert not r.ok and r.reason == tg.ACTOR_INACTIVE


# --- enforce_or_log : gating par flags --------------------------------------

def test_flags_off_never_cancels(world, settings):
    settings.OWNER_SCOPE_ENABLED = False; settings.OWNER_SCOPE_AUDIT_ONLY = False
    world["member_a"].delete()  # accès cassé, mais flags off
    proceed, reason = tg.enforce_or_log(user_id=world["um"].id, rag_space_id=world["rs_a"].id,
                                        required_permission="read", job_label="t")
    assert proceed is True and reason == tg.FLAGS_OFF


def test_audit_only_observes_but_proceeds(world, settings):
    settings.OWNER_SCOPE_ENABLED = False; settings.OWNER_SCOPE_AUDIT_ONLY = True
    world["member_a"].delete()
    proceed, reason = tg.enforce_or_log(user_id=world["um"].id, rag_space_id=world["rs_a"].id,
                                        required_permission="read", job_label="t")
    assert proceed is True and reason == tg.MEMBERSHIP_MISSING  # observé mais pas bloquant


def test_enforced_cancels(world, enforced):
    world["member_a"].delete()
    proceed, reason = tg.enforce_or_log(user_id=world["um"].id, rag_space_id=world["rs_a"].id,
                                        required_permission="read", job_label="t")
    assert proceed is False and reason == tg.MEMBERSHIP_MISSING


# --- Tâche réelle : résumé annulé à l'exécution -----------------------------

def test_summary_task_cancelled_after_revocation(world, enforced):
    from apps.rag_core.tasks import generate_document_summary_task
    summary = DocumentSummary.objects.create(
        rag_space=world["rs_a"], document=world["doc_a"], variant_key="v", status="PENDING",
    )
    world["member_a"].delete()  # l'acteur perd l'accès entre enqueue et exécution
    res = generate_document_summary_task.apply(
        kwargs={"summary_id": str(summary.id), "user_id": world["um"].id}
    ).get()
    assert res["ok"] is False and res["error"] == tg.MEMBERSHIP_MISSING
    summary.refresh_from_db()
    assert summary.status == "FAILED"          # pas de résultat partiel
    assert summary.summary == ""               # rien généré


# --- Invariant cache : permission AVANT lecture -----------------------------

def test_cache_read_after_permission_membership_removed(world):
    # legacy : sans owner_scope, l'accès document dérive du membership workspace.
    client = APIClient(); client.force_authenticate(user=world["um"])
    r1 = client.get(f"/api/v1/documents/{world['doc_a'].id}/")
    assert r1.status_code == 200                       # accès initial
    world["member_a"].delete()                         # révocation
    r2 = client.get(f"/api/v1/documents/{world['doc_a'].id}/")
    assert r2.status_code == 404                        # 404 AVANT tout service de cache
