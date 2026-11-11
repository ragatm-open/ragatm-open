"""Tests HTTP des quotas documentaires par workspace.

Couvre : création (défauts / custom / refus), endpoint de modification superuser
(matrice d'autorisation, PATCH partiel, réduction sous usage), lecture owner/admin,
endpoint de défauts, et enforcement à l'upload (409 structuré). L'audit est vérifié
sur la création et la modification.
"""
import hashlib
import io

import pytest
from rest_framework.test import APIClient

from django.contrib.auth import get_user_model

from apps.audit.models import ActionAuditEvent
from apps.rag_core.models import Document, RagSpace
from apps.workspaces.models import Membership, Workspace, WorkspaceQuota
from apps.workspaces.services import quota as quota_service

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
    return User.objects.create_user("q_su", "q_su@t.local", "x", is_superuser=True, is_staff=True)


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="Q-WS")
    quota_service.ensure_quota(ws, max_document_count=1000, max_document_bytes=10 * 1024 ** 3)
    owner_u = User.objects.create_user("q_owner", "q_owner@t.local", "x")
    admin_u = User.objects.create_user("q_admin", "q_admin@t.local", "x")
    member_u = User.objects.create_user("q_member", "q_member@t.local", "x")
    owner = Membership.objects.create(workspace=ws, user=owner_u, role="owner")
    Membership.objects.create(workspace=ws, user=admin_u, role="admin")
    Membership.objects.create(workspace=ws, user=member_u, role="member")
    rs = RagSpace.objects.create(workspace=ws, name="RS", owner_scope=owner)
    return {
        "ws": ws, "rs": rs, "owner_u": owner_u, "admin_u": admin_u,
        "member_u": member_u,
    }


def _doc(rs, *, size_bytes=1, status="READY", sha=""):
    return Document.objects.create(
        rag_space=rs, filename="f", size_bytes=size_bytes, status=status, sha256=sha
    )


# --- Création : défauts / custom / refus ------------------------------------

def test_create_uses_platform_defaults(su, settings):
    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT = 777
    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES = 8888
    u = User.objects.create_user("o1", "o1@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/",
                      {"name": "W", "owner_user_id": u.id}, format="json", **H)
    assert r.status_code == 201, r.data
    assert r.data["quota"] == {"max_document_count": 777, "max_document_bytes": 8888}
    q = WorkspaceQuota.objects.get(workspace_id=r.data["workspace"]["id"])
    assert q.max_document_count == 777 and q.max_document_bytes == 8888


def test_create_with_custom_quota(su):
    u = User.objects.create_user("o2", "o2@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/", {
        "name": "W2", "owner_user_id": u.id,
        "quota": {"max_document_count": 5, "max_document_bytes": 123456},
    }, format="json", **H)
    assert r.status_code == 201
    q = WorkspaceQuota.objects.get(workspace_id=r.data["workspace"]["id"])
    assert (q.max_document_count, q.max_document_bytes) == (5, 123456)


def test_create_partial_quota_uses_default_for_missing(su, settings):
    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES = 4242
    u = User.objects.create_user("o3", "o3@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/", {
        "name": "W3", "owner_user_id": u.id,
        "quota": {"max_document_count": 9},
    }, format="json", **H)
    assert r.status_code == 201
    q = WorkspaceQuota.objects.get(workspace_id=r.data["workspace"]["id"])
    assert (q.max_document_count, q.max_document_bytes) == (9, 4242)


@pytest.mark.parametrize("bad", [0, -5])
def test_create_rejects_non_positive_quota(su, bad):
    u = User.objects.create_user(f"ob{bad}", f"ob{bad}@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/", {
        "name": "WB", "owner_user_id": u.id,
        "quota": {"max_document_count": bad, "max_document_bytes": 10},
    }, format="json", **H)
    assert r.status_code == 400
    # Aucun workspace créé (rollback / refus avant transaction).
    assert not Workspace.objects.filter(name="WB").exists()


def test_create_rejects_above_hard_cap(su, settings):
    settings.WORKSPACE_HARD_MAX_DOCUMENT_COUNT = 100
    u = User.objects.create_user("ohc", "ohc@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/", {
        "name": "WHC", "owner_user_id": u.id,
        "quota": {"max_document_count": 101, "max_document_bytes": 10},
    }, format="json", **H)
    assert r.status_code == 400 and r.data["detail"] == "exceeds_hard_cap"


def test_create_backward_compatible_without_quota(su):
    u = User.objects.create_user("obc", "obc@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/",
                      {"name": "WBC", "owner_user_id": u.id}, format="json", **H)
    assert r.status_code == 201
    assert WorkspaceQuota.objects.filter(workspace_id=r.data["workspace"]["id"]).exists()


def test_create_emits_quota_created_audit(su):
    u = User.objects.create_user("oaudit", "oaudit@t.local", "x")
    r = _cli(su).post("/api/v1/platform/workspaces/", {
        "name": "WA", "owner_user_id": u.id,
        "quota": {"max_document_count": 7, "max_document_bytes": 700},
    }, format="json", **H)
    ev = ActionAuditEvent.objects.filter(action_name="workspace.quota.created").order_by("-created_at").first()
    assert ev is not None
    assert ev.workspace_id == str(r.data["workspace"]["id"])
    assert ev.metadata["new_max_document_count"] == 7


# --- Modification (PATCH) : permissions -------------------------------------

def test_patch_quota_superuser_ok(su, world):
    r = _cli(su).patch(f"/api/v1/platform/workspaces/{world['ws'].id}/quota/",
                       {"max_document_count": 42}, format="json", **H)
    assert r.status_code == 200, r.data
    assert r.data["quota"]["max_document_count"] == 42
    assert WorkspaceQuota.objects.get(workspace=world["ws"]).max_document_count == 42


def test_patch_quota_forbidden_for_non_superuser(world):
    url = f"/api/v1/platform/workspaces/{world['ws'].id}/quota/"
    body = {"max_document_count": 5}
    assert _cli(world["owner_u"]).patch(url, body, format="json", **H).status_code == 403
    assert _cli(world["admin_u"]).patch(url, body, format="json", **H).status_code == 403
    assert _cli(world["member_u"]).patch(url, body, format="json", **H).status_code == 403
    assert _cli().patch(url, body, format="json", **H).status_code == 401
    # Aucune modification, aucun event de refus.
    assert WorkspaceQuota.objects.get(workspace=world["ws"]).max_document_count == 1000
    assert not ActionAuditEvent.objects.filter(action_name="workspace.quota.updated").exists()


def test_patch_partial_keeps_other_limit(su, world):
    before = WorkspaceQuota.objects.get(workspace=world["ws"])
    r = _cli(su).patch(f"/api/v1/platform/workspaces/{world['ws'].id}/quota/",
                       {"max_document_bytes": 999}, format="json", **H)
    assert r.status_code == 200
    q = WorkspaceQuota.objects.get(workspace=world["ws"])
    assert q.max_document_bytes == 999
    assert q.max_document_count == before.max_document_count  # inchangé


def test_patch_rejects_unknown_field(su, world):
    r = _cli(su).patch(f"/api/v1/platform/workspaces/{world['ws'].id}/quota/",
                       {"max_document_count": 5, "evil": 1}, format="json", **H)
    assert r.status_code == 400 and r.data["detail"] == "unknown_fields"


def test_patch_rejects_zero(su, world):
    r = _cli(su).patch(f"/api/v1/platform/workspaces/{world['ws'].id}/quota/",
                       {"max_document_count": 0}, format="json", **H)
    assert r.status_code == 400


def test_patch_workspace_not_found(su):
    r = _cli(su).patch("/api/v1/platform/workspaces/999999/quota/",
                       {"max_document_count": 5}, format="json", **H)
    assert r.status_code == 404


def test_patch_reduction_below_usage_warns_and_keeps_docs(su, world):
    _doc(world["rs"], size_bytes=200)
    _doc(world["rs"], size_bytes=200)  # used bytes = 400, count = 2
    r = _cli(su).patch(f"/api/v1/platform/workspaces/{world['ws'].id}/quota/",
                       {"max_document_bytes": 100}, format="json", **H)
    assert r.status_code == 200
    assert r.data["quota"]["is_size_exceeded"] is True
    assert "warning" in r.data
    # Aucun document supprimé.
    assert Document.objects.filter(rag_space=world["rs"]).count() == 2


def test_patch_emits_updated_audit_with_old_and_new(su, world):
    _cli(su).patch(f"/api/v1/platform/workspaces/{world['ws'].id}/quota/",
                   {"max_document_count": 50, "reason": "extension"}, format="json", **H)
    ev = ActionAuditEvent.objects.filter(action_name="workspace.quota.updated").first()
    assert ev is not None
    assert ev.metadata["old_max_document_count"] == 1000
    assert ev.metadata["new_max_document_count"] == 50
    assert ev.metadata["reason"] == "extension"


# --- Lecture (owner/admin) --------------------------------------------------

def test_get_quota_owner_and_admin_ok(world):
    url = f"/api/v1/workspaces/{world['ws'].id}/quota/"
    assert _cli(world["owner_u"]).get(url, **H).status_code == 200
    r = _cli(world["admin_u"]).get(url, **H)
    assert r.status_code == 200
    assert set(["max_document_count", "used_document_count", "remaining_document_bytes",
                "is_exceeded", "can_upload"]).issubset(r.data.keys())


def test_get_quota_forbidden_for_member_and_anon(world):
    url = f"/api/v1/workspaces/{world['ws'].id}/quota/"
    assert _cli(world["member_u"]).get(url, **H).status_code == 403
    assert _cli().get(url, **H).status_code == 401


# --- Défauts de plateforme (endpoint) ---------------------------------------

def test_defaults_endpoint(su, settings):
    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT = 111
    settings.WORKSPACE_HARD_MAX_DOCUMENT_COUNT = 222
    r = _cli(su).get("/api/v1/platform/workspace-quota-defaults/", **H)
    assert r.status_code == 200
    assert r.data["default_max_document_count"] == 111
    assert r.data["hard_max_document_count"] == 222


def test_defaults_endpoint_forbidden_for_non_superuser(world):
    assert _cli(world["owner_u"]).get("/api/v1/platform/workspace-quota-defaults/", **H).status_code == 403


# --- Enforcement à l'upload -------------------------------------------------

def _txt(content: bytes):
    f = io.BytesIO(content)
    f.name = "note.txt"
    return f


def test_upload_blocked_when_count_quota_reached(world):
    WorkspaceQuota.objects.filter(workspace=world["ws"]).update(max_document_count=1)
    _doc(world["rs"], size_bytes=1)  # used=1 == max
    r = _cli(world["owner_u"]).post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(world["rs"].id),
              "file": _txt(b"Paris est la capitale de la France. Contenu suffisant.")},
        format="multipart", **H,
    )
    assert r.status_code == 409, r.data
    assert r.data["code"] == "workspace_file_count_quota_exceeded"
    assert r.data["quota"]["max"] == 1


def test_upload_blocked_when_size_quota_reached(world):
    WorkspaceQuota.objects.filter(workspace=world["ws"]).update(max_document_bytes=10)
    _doc(world["rs"], size_bytes=10)  # used bytes == max
    r = _cli(world["owner_u"]).post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(world["rs"].id),
              "file": _txt(b"Paris est la capitale de la France. Contenu largement suffisant.")},
        format="multipart", **H,
    )
    assert r.status_code == 409
    assert r.data["code"] == "workspace_storage_quota_exceeded"


def test_upload_allowed_under_quota(world):
    r = _cli(world["owner_u"]).post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(world["rs"].id),
              "file": _txt(b"Paris est la capitale de la France. Contenu suffisant.")},
        format="multipart", **H,
    )
    assert r.status_code == 200, r.data


def test_upload_dedup_does_not_consume_quota(world):
    content = b"Paris est la capitale de la France. Contenu suffisant pour dedup."
    sha = hashlib.sha256(content).hexdigest()
    WorkspaceQuota.objects.filter(workspace=world["ws"]).update(max_document_count=1)
    _doc(world["rs"], size_bytes=len(content), sha=sha)  # used=1 == max, même sha
    r = _cli(world["owner_u"]).post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(world["rs"].id), "file": _txt(content)},
        format="multipart", **H,
    )
    # Dédup : pas de 409, aucune nouvelle ligne consommée.
    assert r.status_code == 200, r.data
    assert Document.objects.filter(rag_space=world["rs"]).count() == 1
