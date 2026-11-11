"""Lot câblage enforcement — la décision HTTP effective applique owner_scope.

Trois régimes de flags :
  - OFF (défaut) : couvert par les suites legacy (inchangé) ;
  - owner_scope enforced (ACL audit-only) : cross-owner bloqué (404 anti-énum) ;
  - ACL enforced : rôles d'espace réellement bloquants.
"""
import json
import logging

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import Document, RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="WS-ENF")
    ua = User.objects.create_user("enf_oa", "enf_oa@t.local", "x")
    ub = User.objects.create_user("enf_ob", "enf_ob@t.local", "x")
    uad = User.objects.create_user("enf_ad", "enf_ad@t.local", "x")
    um = User.objects.create_user("enf_ma", "enf_ma@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
    Membership.objects.create(workspace=ws, user=uad, role="admin", owner_scope=owner_a)
    Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner_a)
    rs_a = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner_a)
    rs_b = RagSpace.objects.create(workspace=ws, name="B", owner_scope=owner_b)
    doc_a = Document.objects.create(rag_space=rs_a, filename="a.pdf", sha256="a" * 64)
    doc_b = Document.objects.create(rag_space=rs_b, filename="b.pdf", sha256="b" * 64)
    return dict(ws=ws, ua=ua, ub=ub, uad=uad, um=um, owner_a=owner_a,
                rs_a=rs_a, rs_b=rs_b, doc_a=doc_a, doc_b=doc_b)


def cli(u):
    c = APIClient(); c.force_authenticate(user=u); return c


@pytest.fixture
def owner_scope_enforced(settings):
    # Config d'activation staging : owner_scope bloquant, ACL en observation.
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False
    settings.RAGSPACE_ACL_AUDIT_ONLY = True


# --- owner_scope enforced (ACL audit-only) ----------------------------------

def test_owner_sees_own_space_denies_cross(world, owner_scope_enforced):
    c = cli(world["ua"])
    assert c.get(f"/api/v1/rag-spaces/{world['rs_a'].id}/stats/").status_code == 200
    assert c.get(f"/api/v1/rag-spaces/{world['rs_b'].id}/stats/").status_code == 404  # anti-énum


def test_admin_and_member_denied_cross_perimeter(world, owner_scope_enforced):
    assert cli(world["uad"]).get(f"/api/v1/rag-spaces/{world['rs_b'].id}/stats/").status_code == 404
    assert cli(world["um"]).get(f"/api/v1/rag-spaces/{world['rs_b'].id}/stats/").status_code == 404


def test_list_returns_only_own_perimeter(world, owner_scope_enforced):
    r = cli(world["ua"]).get(f"/api/v1/rag-spaces/?workspace_id={world['ws'].id}")
    assert r.status_code == 200
    ids = {x["id"] for x in r.data}
    assert str(world["rs_a"].id) in ids and str(world["rs_b"].id) not in ids  # liste FILTRÉE


def test_document_cross_perimeter_404(world, owner_scope_enforced):
    c = cli(world["ua"])
    assert c.get(f"/api/v1/documents/{world['doc_a'].id}/").status_code == 200
    assert c.get(f"/api/v1/documents/{world['doc_b'].id}/").status_code == 404  # IDOR bloqué
    assert c.get(f"/api/v1/documents/?rag_space_id={world['rs_b'].id}").status_code == 404


def test_core_retrieve_cross_perimeter_404(world, owner_scope_enforced):
    r = cli(world["ua"]).post("/api/v1/core/retrieve/",
                              {"rag_space_id": str(world["rs_b"].id), "question": "x"}, format="json")
    assert r.status_code == 404


def test_analyses_create_cross_perimeter_404(world, owner_scope_enforced):
    r = cli(world["ua"]).post(f"/api/v1/rag-spaces/{world['rs_b'].id}/analyses/",
                              {"analysis_type": "whatever"}, format="json")
    assert r.status_code == 404  # require lève AVANT la validation du type


def test_member_no_grant_still_allowed_under_owner_scope_only(world, owner_scope_enforced, caplog):
    # ACL en audit-only : le membre du bon périmètre reste AUTORISÉ par owner_scope,
    # mais l'audit ACL signale access_missing (observé, non bloquant).
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        r = cli(world["um"]).get(f"/api/v1/rag-spaces/{world['rs_a'].id}/stats/")
    assert r.status_code == 200  # owner_scope OK, ACL non bloquante
    evs = [json.loads(rec.getMessage().split(" ", 1)[1]) for rec in caplog.records
           if rec.name == "owner_scope.audit" and "owner_scope_audit " in rec.getMessage()]
    assert any(e.get("endpoint") == "ragspaces.stats" and e.get("reason") == "access_missing" for e in evs)


# --- ACL enforced ------------------------------------------------------------

@pytest.fixture
def acl_enforced(settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = True
    settings.RAGSPACE_ACL_AUDIT_ONLY = False


def test_acl_member_no_grant_404(world, acl_enforced):
    assert cli(world["um"]).get(f"/api/v1/rag-spaces/{world['rs_a'].id}/stats/").status_code == 404


def test_acl_viewer_grant_read_ok(world, acl_enforced):
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["um"], space_role="viewer")
    assert cli(world["um"]).get(f"/api/v1/rag-spaces/{world['rs_a'].id}/stats/").status_code == 200
    assert cli(world["um"]).get(f"/api/v1/documents/{world['doc_a'].id}/").status_code == 200


def test_acl_viewer_cannot_write_403(world, acl_enforced):
    # viewer a l'accès (visible) mais rôle insuffisant pour écrire -> 403 (pas 404)
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["um"], space_role="viewer")
    r = cli(world["um"]).delete(f"/api/v1/documents/{world['doc_a'].id}/delete/")
    assert r.status_code == 403


def test_acl_owner_implicit_manage(world, acl_enforced):
    # l'owner du périmètre garde un accès complet SANS grant explicite
    assert cli(world["ua"]).get(f"/api/v1/rag-spaces/{world['rs_a'].id}/stats/").status_code == 200
