"""Phase 2 — Modèle de données corpus métier (étanchéité & contraintes)."""
import pytest
from django.db import IntegrityError, transaction

from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusChunk,
    DomainCorpusDocument,
    WorkspaceDomainCorpusAccess,
)
from apps.corpus.models.document import CorpusDocumentStatus
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def test_create_business_domain(business_domain):
    assert business_domain.pk is not None
    assert business_domain.is_active is True


def test_create_domain_corpus(domain_corpus, business_domain):
    assert domain_corpus.domain_id == business_domain.id
    assert domain_corpus.embedding_provider == "ai_gateway"


def test_business_domain_slug_unique(business_domain):
    with pytest.raises(IntegrityError):
        BusinessDomain.objects.create(slug="logistique", name="Finance dup")


def test_domain_corpus_slug_unique_per_domain(domain_corpus, business_domain):
    # même slug dans le même domaine -> interdit
    with pytest.raises(IntegrityError):
        DomainCorpus.objects.create(domain=business_domain, slug="qualite", name="dup")


def test_domain_corpus_same_slug_other_domain_ok(domain_corpus):
    other = BusinessDomain.objects.create(slug="sante", name="Santé")
    # même slug 'amf' mais autre domaine -> autorisé
    c = DomainCorpus.objects.create(domain=other, slug="qualite", name="Autre")
    assert c.pk is not None


def test_document_sha256_unique_in_corpus(domain_corpus):
    DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="a.pdf", sha256="abc",
        status=CorpusDocumentStatus.READY,
    )
    with pytest.raises(IntegrityError):
        DomainCorpusDocument.objects.create(
            corpus=domain_corpus, original_filename="b.pdf", sha256="abc",
            status=CorpusDocumentStatus.READY,
        )


def test_same_sha256_allowed_in_other_corpus(domain_corpus, business_domain):
    DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="a.pdf", sha256="abc",
        status=CorpusDocumentStatus.READY,
    )
    other = DomainCorpus.objects.create(domain=business_domain, slug="autre", name="Autre")
    d = DomainCorpusDocument.objects.create(
        corpus=other, original_filename="a.pdf", sha256="abc",
        status=CorpusDocumentStatus.READY,
    )
    assert d.pk is not None


def test_failed_document_does_not_block_sha256(domain_corpus):
    # un doc FAILED ne réserve pas le sha256 (re-upload possible)
    DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="a.pdf", sha256="zzz",
        status=CorpusDocumentStatus.FAILED,
    )
    d = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="a.pdf", sha256="zzz",
        status=CorpusDocumentStatus.READY,
    )
    assert d.pk is not None


def test_workspace_corpus_association_unique(domain_corpus):
    ws = Workspace.objects.create(name="WAssoc")
    WorkspaceDomainCorpusAccess.objects.create(workspace=ws, corpus=domain_corpus)
    with pytest.raises(IntegrityError):
        WorkspaceDomainCorpusAccess.objects.create(workspace=ws, corpus=domain_corpus)


def test_delete_corpus_cascades_documents_and_chunks(domain_corpus):
    doc = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="a.pdf", sha256="h1",
        status=CorpusDocumentStatus.READY,
    )
    DomainCorpusChunk.objects.create(corpus=domain_corpus, document=doc, chunk_index=0, text="x")
    corpus_id = domain_corpus.id
    domain_corpus.delete()
    assert DomainCorpusDocument.objects.filter(corpus_id=corpus_id).count() == 0
    assert DomainCorpusChunk.objects.filter(corpus_id=corpus_id).count() == 0


def test_delete_workspace_keeps_shared_corpus(domain_corpus):
    ws = Workspace.objects.create(name="WGone")
    WorkspaceDomainCorpusAccess.objects.create(workspace=ws, corpus=domain_corpus)
    corpus_id = domain_corpus.id
    ws.delete()
    # l'association disparaît, mais le corpus partagé reste intact
    assert WorkspaceDomainCorpusAccess.objects.filter(corpus_id=corpus_id).count() == 0
    assert DomainCorpus.objects.filter(id=corpus_id).exists()
