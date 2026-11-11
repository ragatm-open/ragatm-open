"""Phases 6 & 7 — Retrieval bi-source + typage des sources (chat & logs).

Garanties critiques (sécurité multi-tenant) :
- un workspace utilise ses docs privés + les corpus AUTORISÉS ;
- deux workspaces partageant un corpus ne voient jamais leurs docs privés respectifs ;
- un workspace NON associé ne récupère aucun chunk corpus ;
- toutes les sources sont typées (workspace_document / domain_corpus), en réponse ET en log.
"""
import pytest

from apps.audit.models import RagQueryLog
from apps.corpus.models import DomainCorpus, DomainCorpusDocument
from apps.corpus.services.ingest import ingest_corpus_document_from_upload
from apps.rag_core.services.query import query_rag
from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db

CORPUS_TEXT = b"Reglementation normes: le credit fournisseur partage est encadre par la procedure."


def _mk_workspace(name, username, private_text, ingest_text):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    u = U.objects.create_user(username=username, password="pw", email=f"{username}@e.com")
    ws = Workspace.objects.create(name=name)
    Membership.objects.create(workspace=ws, user=u, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(name=f"rs-{name}", workspace=ws)
    ingest_text(rs, private_text, filename=f"prive-{name}.txt")
    return {"user": u, "workspace": ws, "rag_space": rs}


@pytest.fixture
def shared_corpus(db, business_domain, grant_access):
    corpus = DomainCorpus.objects.create(domain=business_domain, slug="logistique-corpus", name="Procédures qualité")
    ingest_corpus_document_from_upload(
        corpus=corpus, filename="qualite.txt", content_type="text/plain", file_bytes=CORPUS_TEXT
    )
    return corpus


def _source_types(result):
    return {s.get("source_type") for s in result.get("sources", [])}


def test_workspace_with_corpus_uses_private_plus_corpus(shared_corpus, grant_access, ingest_text):
    a = _mk_workspace("A", "alice", "Procedure interne de credit Alpha pour le workspace A.", ingest_text)
    grant_access(workspace=a["workspace"], corpus=shared_corpus)

    res = query_rag(
        rag_space_id=a["rag_space"].id, question="credit", user=a["user"], rag_space=a["rag_space"]
    )
    assert res["status"] == "ok"
    types = _source_types(res)
    assert "workspace_document" in types
    assert "domain_corpus" in types
    # la source corpus porte bien les métadonnées de provenance
    corpus_sources = [s for s in res["sources"] if s.get("source_type") == "domain_corpus"]
    assert corpus_sources and corpus_sources[0]["source"]["corpus_name"] == "Procédures qualité"


def test_two_workspaces_share_corpus_without_private_leak(shared_corpus, grant_access, ingest_text):
    a = _mk_workspace("A2", "alice2", "credit Alpha confidentiel workspace A2.", ingest_text)
    b = _mk_workspace("B2", "bob2", "credit Bravo confidentiel workspace B2.", ingest_text)
    grant_access(workspace=a["workspace"], corpus=shared_corpus)
    grant_access(workspace=b["workspace"], corpus=shared_corpus)

    res_a = query_rag(rag_space_id=a["rag_space"].id, question="credit", user=a["user"], rag_space=a["rag_space"])
    res_b = query_rag(rag_space_id=b["rag_space"].id, question="credit", user=b["user"], rag_space=b["rag_space"])

    blob_a = str(res_a["sources"])
    blob_b = str(res_b["sources"])
    # A ne voit jamais le privé de B et inversement
    assert "Bravo" not in blob_a
    assert "Alpha" not in blob_b
    # mais les deux accèdent au corpus partagé
    assert "domain_corpus" in _source_types(res_a)
    assert "domain_corpus" in _source_types(res_b)


def test_workspace_without_corpus_gets_no_corpus_chunks(shared_corpus, ingest_text):
    # workspace C : corpus existe mais N'EST PAS associé
    c = _mk_workspace("C", "carol", "credit interne workspace C.", ingest_text)
    res = query_rag(rag_space_id=c["rag_space"].id, question="credit", user=c["user"], rag_space=c["rag_space"])
    assert "domain_corpus" not in _source_types(res)
    # le privé fonctionne toujours
    assert "workspace_document" in _source_types(res)


def test_disabled_association_excluded_from_retrieval(shared_corpus, grant_access, ingest_text):
    d = _mk_workspace("D", "dan", "credit interne workspace D.", ingest_text)
    grant_access(workspace=d["workspace"], corpus=shared_corpus, enabled=False)
    res = query_rag(rag_space_id=d["rag_space"].id, question="credit", user=d["user"], rag_space=d["rag_space"])
    assert "domain_corpus" not in _source_types(res)


def test_log_sources_are_typed(shared_corpus, grant_access, ingest_text):
    a = _mk_workspace("E", "erin", "credit interne workspace E.", ingest_text)
    grant_access(workspace=a["workspace"], corpus=shared_corpus)
    res = query_rag(rag_space_id=a["rag_space"].id, question="credit", user=a["user"], rag_space=a["rag_space"])

    log = RagQueryLog.objects.get(id=res["log_id"])
    assert log.sources_schema_version == 3   # v3 : citation Excel auditable (lot E3)
    types = {s.get("source_type") for s in log.sources}
    assert "domain_corpus" in types
    assert "workspace_document" in types
    for s in log.sources:
        assert s["source_type"] in ("workspace_document", "domain_corpus")
        if s["source_type"] == "domain_corpus":
            assert "corpus_id" in s and "business_domain_name" in s
