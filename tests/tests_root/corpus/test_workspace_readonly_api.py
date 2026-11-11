"""Phase 4 — API lecture seule côté workspace.

Un membre voit les corpus ASSOCIÉS+ACTIFS de SON workspace, jamais ceux d'un
autre, jamais les non-associés, et ne dispose d'aucune écriture.
"""
import pytest

from apps.corpus.models import DomainCorpus

pytestmark = pytest.mark.django_db


def _url(ws_id):
    return f"/api/v1/workspaces/{ws_id}/domain-corpora/"


def test_member_lists_assigned_corpora(api_client, workspace, domain_corpus, grant_access):
    grant_access(workspace=workspace, corpus=domain_corpus)
    res = api_client.get(_url(workspace.id))
    assert res.status_code == 200
    assert len(res.data) == 1
    item = res.data[0]
    assert item["corpus_id"] == str(domain_corpus.id)
    assert item["read_only"] is True
    assert item["business_domain_name"] == "Logistique"


def test_workspace_without_corpus_returns_empty(api_client, workspace):
    res = api_client.get(_url(workspace.id))
    assert res.status_code == 200
    assert res.data == []


def test_non_member_denied(foreign_setup, workspace, domain_corpus, grant_access):
    grant_access(workspace=workspace, corpus=domain_corpus)
    # foreign_setup user n'est pas membre de `workspace`
    res = foreign_setup["client"].get(_url(workspace.id))
    assert res.status_code == 403


def test_unassigned_corpus_not_listed(api_client, workspace, domain_corpus):
    # corpus existe mais n'est PAS associé au workspace
    res = api_client.get(_url(workspace.id))
    assert res.status_code == 200
    assert res.data == []


def test_disabled_association_not_listed(api_client, workspace, domain_corpus, grant_access):
    grant_access(workspace=workspace, corpus=domain_corpus, enabled=False)
    res = api_client.get(_url(workspace.id))
    assert res.status_code == 200
    assert res.data == []


def test_inactive_corpus_not_listed(api_client, workspace, domain_corpus, grant_access):
    grant_access(workspace=workspace, corpus=domain_corpus)
    domain_corpus.is_active = False
    domain_corpus.save(update_fields=["is_active"])
    res = api_client.get(_url(workspace.id))
    assert res.status_code == 200
    assert res.data == []


def test_no_sensitive_fields_exposed(api_client, workspace, domain_corpus, grant_access):
    grant_access(workspace=workspace, corpus=domain_corpus)
    res = api_client.get(_url(workspace.id))
    blob = str(res.data).lower()
    assert "storage" not in blob
    assert "embedding_model" not in res.data[0]


def test_other_workspace_corpus_not_visible(api_client, workspace, business_domain, grant_access):
    """Un corpus associé à un AUTRE workspace n'apparaît pas ici."""
    from apps.workspaces.models import Workspace

    other_ws = Workspace.objects.create(name="WOther")
    other_corpus = DomainCorpus.objects.create(domain=business_domain, slug="other", name="Other")
    grant_access(workspace=other_ws, corpus=other_corpus)
    res = api_client.get(_url(workspace.id))
    assert res.status_code == 200
    assert res.data == []
