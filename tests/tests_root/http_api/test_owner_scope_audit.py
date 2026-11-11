"""Lot 2 — enforcement en AUDIT_ONLY (observation).

Invariant prouvé : réponse HTTP = décision LEGACY (inchangée) ; journal =
décision CIBLE. Matrice à deux owners dans le MÊME workspace + cas IDOR document.
Vérifie aussi : classification (matched/would_*), et silence total flags OFF.
"""
import json
import logging

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import Document, RagSpace
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import scope_audit

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def two_owners(db):
    ws = Workspace.objects.create(name="WS-2OWN")
    ua = User.objects.create_user("oa", "oa@t.local", "x")
    ub = User.objects.create_user("ob", "ob@t.local", "x")
    uv = User.objects.create_user("va", "va@t.local", "x")
    un = User.objects.create_user("nomember", "nm@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
    Membership.objects.create(workspace=ws, user=uv, role="viewer", owner_scope=owner_a)
    rs_a = RagSpace.objects.create(workspace=ws, name="A1", owner_scope=owner_a)
    rs_b = RagSpace.objects.create(workspace=ws, name="B1", owner_scope=owner_b)
    doc_b = Document.objects.create(rag_space=rs_b, filename="b.pdf", sha256="b" * 64)
    return dict(ws=ws, ua=ua, ub=ub, uv=uv, un=un, owner_a=owner_a, owner_b=owner_b,
                rs_a=rs_a, rs_b=rs_b, doc_b=doc_b)


@pytest.fixture
def audit_only(settings):
    settings.OWNER_SCOPE_ENABLED = False
    settings.OWNER_SCOPE_AUDIT_ONLY = True
    settings.RAGSPACE_ACL_ENABLED = False
    settings.RAGSPACE_ACL_AUDIT_ONLY = False
    return settings


def _events(caplog):
    out = []
    for rec in caplog.records:
        if rec.name == "owner_scope.audit" and "owner_scope_audit " in rec.getMessage():
            out.append(json.loads(rec.getMessage().split(" ", 1)[1]))
    return out


# --- Emitter matrix (les 4 cellules) ----------------------------------------

def test_matrix_matched_allow(two_owners, audit_only, caplog):
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        scope_audit.emit_ragspace_decision(user=two_owners["ua"], rag_space_id=two_owners["rs_a"].id,
                                            permission="read", endpoint="t")
    e = _events(caplog)[-1]
    assert e["legacy_decision"] == "allow" and e["target_decision"] == "allow"
    assert e["audit"] == "matched_allow"


def test_matrix_would_deny_cross_owner(two_owners, audit_only, caplog):
    # owner_a lit un espace du périmètre B (même workspace) : legacy allow, cible deny
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        scope_audit.emit_ragspace_decision(user=two_owners["ua"], rag_space_id=two_owners["rs_b"].id,
                                            permission="read", endpoint="t")
    e = _events(caplog)[-1]
    assert e["legacy_decision"] == "allow" and e["target_decision"] == "deny"
    assert e["audit"] == "would_deny" and e["reason"] == "owner_scope_mismatch"


def test_matrix_would_allow_viewer_write(two_owners, audit_only, caplog):
    # viewer écrit dans son périmètre : legacy DENY (viewer), cible ALLOW (scope ok, ACL off)
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        scope_audit.emit_ragspace_decision(user=two_owners["uv"], rag_space_id=two_owners["rs_a"].id,
                                            permission="write", endpoint="t")
    e = _events(caplog)[-1]
    assert e["legacy_decision"] == "deny" and e["target_decision"] == "allow"
    assert e["audit"] == "would_allow"


def test_matrix_matched_deny_non_member(two_owners, audit_only, caplog):
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        scope_audit.emit_ragspace_decision(user=two_owners["un"], rag_space_id=two_owners["rs_a"].id,
                                            permission="read", endpoint="t")
    e = _events(caplog)[-1]
    assert e["legacy_decision"] == "deny" and e["target_decision"] == "deny"
    assert e["audit"] == "matched_deny"


# --- HTTP : réponse legacy INCHANGÉE + audit correct ------------------------

def test_ragspace_list_http_unchanged_and_audited(two_owners, audit_only, caplog):
    client = APIClient(); client.force_authenticate(user=two_owners["ua"])
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        res = client.get(f"/api/v1/rag-spaces/?workspace_id={two_owners['ws'].id}")
    # Legacy inchangé : owner_a voit TOUJOURS les 2 espaces du workspace
    assert res.status_code == 200
    ids = {r["id"] for r in res.data}
    assert {str(two_owners["rs_a"].id), str(two_owners["rs_b"].id)} <= ids
    # Audit : la cible n'en montrerait qu'un (masquerait rs_b)
    lst = [e for e in _events(caplog) if e["event"] == "ragspace_list_decision"]
    assert lst and lst[-1]["legacy_count"] == 2 and lst[-1]["target_count"] == 1
    assert lst[-1]["would_hide_count"] == 1


def test_document_idor_http_unchanged_and_would_deny(two_owners, audit_only, caplog):
    # owner_a connaît l'UUID d'un document du périmètre B → legacy 200, cible would_deny
    client = APIClient(); client.force_authenticate(user=two_owners["ua"])
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        res = client.get(f"/api/v1/documents/{two_owners['doc_b'].id}/")
    assert res.status_code == 200  # réponse legacy INCHANGÉE en audit-only
    docevents = [e for e in _events(caplog) if e["endpoint"] == "documents.access"]
    assert docevents and docevents[-1]["audit"] == "would_deny"
    assert docevents[-1]["rag_space_id"] == str(two_owners["rs_b"].id)


# --- Surfaces complémentaires (threads) : câblées & IDOR observé -------------

def test_chat_thread_list_idor_audited(two_owners, audit_only, caplog):
    # owner_a liste les threads d'un espace du périmètre B : legacy 200 (membre),
    # cible would_deny (cross-périmètre). Réponse HTTP inchangée.
    client = APIClient(); client.force_authenticate(user=two_owners["ua"])
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        res = client.get(f"/api/v1/chat/threads/?rag_space_id={two_owners['rs_b'].id}")
    assert res.status_code == 200
    evs = [e for e in _events(caplog) if e["endpoint"] == "chat.threads.list"]
    assert evs and evs[-1]["audit"] == "would_deny"


# --- Flags OFF : aucun événement owner-scope --------------------------------

def test_flags_off_no_audit_event(two_owners, settings, caplog):
    settings.OWNER_SCOPE_ENABLED = False
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False
    settings.RAGSPACE_ACL_AUDIT_ONLY = False
    client = APIClient(); client.force_authenticate(user=two_owners["ua"])
    with caplog.at_level(logging.INFO, logger="owner_scope.audit"):
        res = client.get(f"/api/v1/rag-spaces/?workspace_id={two_owners['ws'].id}")
        scope_audit.emit_ragspace_decision(user=two_owners["ua"], rag_space_id=two_owners["rs_a"].id,
                                            permission="read", endpoint="t")
    assert res.status_code == 200
    assert _events(caplog) == []  # silence total
