"""Phase 3 — API admin globale (permissions + CRUD + associations).

Sécurité : tous les endpoints sont réservés au propriétaire global ragATM.
Un workspace admin/member/viewer reçoit 403, un anonyme 401.
"""
import pytest

from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    WorkspaceDomainCorpusAccess,
)
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def test_global_owner_can_list_domains(global_owner_client):
    res = global_owner_client.get("/api/v1/admin/business-domains/")
    assert res.status_code == 200


def test_anonymous_denied(client):
    res = client.get("/api/v1/admin/business-domains/")
    assert res.status_code == 401


def test_workspace_admin_denied_on_all_write_endpoints(ws_admin, business_domain, domain_corpus):
    c = ws_admin["client"]
    ws = ws_admin["workspace"]
    cases = [
        ("get", "/api/v1/admin/business-domains/"),
        ("post", "/api/v1/admin/business-domains/"),
        ("get", "/api/v1/admin/domain-corpora/"),
        ("post", "/api/v1/admin/domain-corpora/"),
        ("get", f"/api/v1/admin/domain-corpora/{domain_corpus.id}/documents/"),
        ("post", f"/api/v1/admin/domain-corpora/{domain_corpus.id}/reindex/"),
        ("get", f"/api/v1/admin/workspaces/{ws.id}/domain-corpora/"),
        ("post", f"/api/v1/admin/workspaces/{ws.id}/domain-corpora/"),
    ]
    for method, url in cases:
        res = getattr(c, method)(url, {}, format="json")
        assert res.status_code == 403, f"{method} {url} -> {res.status_code}"


def test_member_and_viewer_denied(api_client, viewer_setup, domain_corpus):
    # api_client = member ; viewer_setup["client"] = viewer
    for c in (api_client, viewer_setup["client"]):
        assert c.get("/api/v1/admin/domain-corpora/").status_code == 403
        assert c.post(f"/api/v1/admin/domain-corpora/{domain_corpus.id}/reindex/").status_code == 403


# ---------------------------------------------------------------------------
# CRUD domaines / corpus
# ---------------------------------------------------------------------------
def test_global_owner_can_create_business_domain(global_owner_client):
    res = global_owner_client.post(
        "/api/v1/admin/business-domains/", {"slug": "juridique", "name": "Juridique"}, format="json"
    )
    assert res.status_code == 201
    assert res.data["slug"] == "juridique"
    assert BusinessDomain.objects.filter(slug="juridique").exists()


def test_create_domain_duplicate_slug_conflict(global_owner_client, business_domain):
    res = global_owner_client.post(
        "/api/v1/admin/business-domains/", {"slug": "logistique", "name": "Dup"}, format="json"
    )
    assert res.status_code == 409


def test_global_owner_can_create_domain_corpus(global_owner_client, business_domain):
    res = global_owner_client.post(
        "/api/v1/admin/domain-corpora/",
        {"domain_id": str(business_domain.id), "slug": "mifid", "name": "MIFID II"},
        format="json",
    )
    assert res.status_code == 201
    assert res.data["domain_id"] == str(business_domain.id)
    assert DomainCorpus.objects.filter(slug="mifid").exists()


def test_create_corpus_unknown_domain_400(global_owner_client):
    import uuid

    res = global_owner_client.post(
        "/api/v1/admin/domain-corpora/",
        {"domain_id": str(uuid.uuid4()), "slug": "x", "name": "X"},
        format="json",
    )
    assert res.status_code == 400


def test_patch_and_delete_corpus(global_owner_client, domain_corpus):
    res = global_owner_client.patch(
        f"/api/v1/admin/domain-corpora/{domain_corpus.id}/", {"name": "Renommé"}, format="json"
    )
    assert res.status_code == 200
    assert res.data["name"] == "Renommé"

    res = global_owner_client.delete(f"/api/v1/admin/domain-corpora/{domain_corpus.id}/")
    assert res.status_code == 204
    assert not DomainCorpus.objects.filter(id=domain_corpus.id).exists()


# ---------------------------------------------------------------------------
# Associations workspace <-> corpus
# ---------------------------------------------------------------------------
def test_global_owner_can_assign_corpus_to_workspace(global_owner_client, domain_corpus):
    ws = Workspace.objects.create(name="WTarget")
    res = global_owner_client.post(
        f"/api/v1/admin/workspaces/{ws.id}/domain-corpora/",
        {"corpus_id": str(domain_corpus.id)},
        format="json",
    )
    assert res.status_code == 201
    assert WorkspaceDomainCorpusAccess.objects.filter(workspace=ws, corpus=domain_corpus, enabled=True).exists()


def test_global_owner_can_remove_association(global_owner_client, domain_corpus, grant_access):
    ws = Workspace.objects.create(name="WTarget2")
    grant_access(workspace=ws, corpus=domain_corpus)
    res = global_owner_client.delete(
        f"/api/v1/admin/workspaces/{ws.id}/domain-corpora/{domain_corpus.id}/"
    )
    assert res.status_code == 204
    assert not WorkspaceDomainCorpusAccess.objects.filter(workspace=ws, corpus=domain_corpus).exists()


def test_workspace_admin_cannot_assign_corpus(ws_admin, domain_corpus):
    ws = ws_admin["workspace"]
    res = ws_admin["client"].post(
        f"/api/v1/admin/workspaces/{ws.id}/domain-corpora/",
        {"corpus_id": str(domain_corpus.id)},
        format="json",
    )
    assert res.status_code == 403
    assert not WorkspaceDomainCorpusAccess.objects.filter(workspace=ws).exists()


def test_serializer_never_exposes_storage_path(global_owner_client, domain_corpus):
    """Anti-fuite : aucun champ 'storage_file'/chemin disque dans les documents."""
    from apps.corpus.models import DomainCorpusDocument
    from apps.corpus.models.document import CorpusDocumentStatus

    DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="a.txt", sha256="h", status=CorpusDocumentStatus.READY
    )
    res = global_owner_client.get(f"/api/v1/admin/domain-corpora/{domain_corpus.id}/documents/")
    assert res.status_code == 200
    assert res.data
    for d in res.data:
        assert "storage_file" not in d
        assert "storage" not in str(d).lower()
