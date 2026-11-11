"""Phase 11/12 — Tests de sécurité ciblés (durcissement multi-tenant).

Couvre les tentatives d'accès illégitimes nommées au cahier des charges :
accès direct par id, réindexation/suppression par un admin de workspace,
retrieval d'un corpus non associé, et anti-énumération (401 vs 403).
"""
import pytest

from apps.corpus.models import DomainCorpusChunk, DomainCorpusDocument
from apps.corpus.models.document import CorpusDocumentStatus

pytestmark = pytest.mark.django_db


def _doc(corpus):
    return DomainCorpusDocument.objects.create(
        corpus=corpus, original_filename="a.txt", sha256="hsec", status=CorpusDocumentStatus.READY
    )


def test_ws_admin_cannot_access_corpus_by_id(ws_admin, domain_corpus):
    res = ws_admin["client"].get(f"/api/v1/admin/domain-corpora/{domain_corpus.id}/")
    assert res.status_code == 403


def test_ws_admin_cannot_reindex_corpus(ws_admin, domain_corpus):
    res = ws_admin["client"].post(f"/api/v1/admin/domain-corpora/{domain_corpus.id}/reindex/")
    assert res.status_code == 403


def test_ws_admin_cannot_delete_corpus_document(ws_admin, domain_corpus):
    doc = _doc(domain_corpus)
    res = ws_admin["client"].delete(f"/api/v1/admin/domain-corpus-documents/{doc.id}/")
    assert res.status_code == 403
    # le document n'a pas été supprimé
    assert DomainCorpusDocument.objects.filter(id=doc.id).exists()


def test_ws_admin_cannot_reindex_corpus_document(ws_admin, domain_corpus):
    doc = _doc(domain_corpus)
    res = ws_admin["client"].post(f"/api/v1/admin/domain-corpus-documents/{doc.id}/reindex/")
    assert res.status_code == 403


def test_anonymous_gets_401_not_403(client, domain_corpus):
    # anti-énumération : un anonyme n'obtient jamais d'indice d'existence (401)
    assert client.get("/api/v1/admin/domain-corpora/").status_code == 401
    assert client.get(f"/api/v1/admin/domain-corpora/{domain_corpus.id}/").status_code == 401


def test_unknown_corpus_id_returns_404_for_owner_only(global_owner_client):
    import uuid

    # le propriétaire global obtient 404 (ressource absente), jamais 500
    res = global_owner_client.get(f"/api/v1/admin/domain-corpora/{uuid.uuid4()}/")
    assert res.status_code == 404


def test_member_cannot_retrieve_unassigned_corpus_via_chat(user, member, workspace, rag_space, domain_corpus, ingest_text):
    """Un corpus non associé ne fournit aucun chunk, même via le chat réel."""
    from apps.corpus.services.ingest import ingest_corpus_document_from_upload
    from apps.rag_core.services.query import query_rag

    ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="qualite.txt", content_type="text/plain",
        file_bytes=b"credit reglementation normes logistique",
    )
    ingest_text(rag_space, "credit interne du workspace", filename="p.txt")

    # PAS d'association corpus<->workspace
    res = query_rag(rag_space_id=rag_space.id, question="credit", user=user, rag_space=rag_space)
    types = {s.get("source_type") for s in res.get("sources", [])}
    assert "domain_corpus" not in types
