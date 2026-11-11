"""Lot 3 (complément) — revalidation de l'INGESTION à l'exécution.

L'ingestion écrit durablement (chunks/embeddings) : `require_context=True`. Sous
enforcement, un acteur non identifiable ou un accès perdu ⇒ REFUS (document
FAILED, aucun chunk). Flags OFF ⇒ historique. AUDIT_ONLY ⇒ observe sans bloquer.
"""
import json
import logging

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.tasks import reindex_document_task
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import task_guard as tg

User = get_user_model()
pytestmark = pytest.mark.django_db

GUARD_REASONS = {
    tg.MEMBERSHIP_MISSING, tg.WORKSPACE_DISABLED, tg.RAGSPACE_DELETED,
    tg.OWNER_SCOPE_MISMATCH, tg.CONTEXT_MISSING, tg.ACTOR_INACTIVE,
}


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="WS-ING")
    ua = User.objects.create_user("ing_oa", "ing_oa@t.local", "x")
    ub = User.objects.create_user("ing_ob", "ing_ob@t.local", "x")
    um = User.objects.create_user("ing_ma", "ing_ma@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
    member_a = Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner_a)
    rs_a = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner_a)
    doc = Document.objects.create(rag_space=rs_a, filename="a.pdf", sha256="a" * 64)
    return dict(ws=ws, ua=ua, um=um, owner_a=owner_a, owner_b=owner_b, member_a=member_a, rs_a=rs_a, doc=doc)


def _run(doc_id, ctx):
    return reindex_document_task.apply(kwargs={"document_id": str(doc_id), "access_context": ctx}).get()


def _ctx(user):
    return {"user_id": user.id, "request_id": "t"}


@pytest.fixture
def enforced(settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False
    settings.RAGSPACE_ACL_AUDIT_ONLY = False


def _is_guard_cancel(res):
    return res.get("ok") is False and res.get("error") in GUARD_REASONS


# --- Passe / refuse selon l'accès -------------------------------------------

def test_ingestion_executes_when_access_unchanged(world, enforced):
    # Accès inchangé : le guard laisse passer (échec ultérieur no_storage = NON-guard).
    res = _run(world["doc"].id, _ctx(world["um"]))
    assert not _is_guard_cancel(res)  # a franchi la garde owner_scope


def test_ingestion_enabled_cancels_after_revocation(world, enforced):
    world["member_a"].delete()
    res = _run(world["doc"].id, _ctx(world["um"]))
    assert res["error"] == tg.MEMBERSHIP_MISSING


def test_ingestion_cancels_after_owner_scope_transfer(world, enforced):
    world["rs_a"].owner_scope = world["owner_b"]; world["rs_a"].save(update_fields=["owner_scope"])
    res = _run(world["doc"].id, _ctx(world["um"]))
    assert res["error"] == tg.OWNER_SCOPE_MISMATCH


def test_ingestion_cancels_after_workspace_disable(world, enforced):
    world["ws"].is_active = False; world["ws"].save(update_fields=["is_active"])
    res = _run(world["doc"].id, _ctx(world["um"]))
    assert res["error"] == tg.WORKSPACE_DISABLED


def test_ingestion_cancels_after_ragspace_delete(world, enforced):
    world["rs_a"].deleted_at = timezone.now(); world["rs_a"].save(update_fields=["deleted_at"])
    res = _run(world["doc"].id, _ctx(world["um"]))
    assert res["error"] == tg.RAGSPACE_DELETED


def test_ingestion_failure_writes_no_chunks_or_embeddings(world, enforced):
    world["member_a"].delete()
    _run(world["doc"].id, _ctx(world["um"]))
    world["doc"].refresh_from_db()
    assert world["doc"].status == "FAILED"
    assert Chunk.objects.filter(document=world["doc"]).count() == 0


# --- Contexte absent : gating par flags -------------------------------------

def test_ingestion_context_missing_continues_when_flags_off(world, settings):
    settings.OWNER_SCOPE_ENABLED = False; settings.OWNER_SCOPE_AUDIT_ONLY = False
    world["member_a"].delete()
    res = _run(world["doc"].id, {"user_id": None, "request_id": "t"})
    assert not _is_guard_cancel(res)  # historique : la garde ne s'active pas


def test_ingestion_context_missing_observed_in_audit_only(world, settings, caplog):
    settings.OWNER_SCOPE_ENABLED = False; settings.OWNER_SCOPE_AUDIT_ONLY = True
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        res = _run(world["doc"].id, {"user_id": None, "request_id": "t"})
    assert not _is_guard_cancel(res)  # audit ne bloque pas
    events = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
              if r.name == "owner_scope.audit" and "owner_scope_audit " in r.getMessage()]
    assert any(e.get("event") == "task_revalidation" and e.get("reason") == tg.CONTEXT_MISSING for e in events)


def test_ingestion_context_missing_rejected_when_enabled(world, enforced):
    res = _run(world["doc"].id, {"user_id": None, "request_id": "t"})
    assert res["error"] == tg.CONTEXT_MISSING
    world["doc"].refresh_from_db()
    assert world["doc"].status == "FAILED"
