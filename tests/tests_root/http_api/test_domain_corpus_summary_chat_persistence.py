"""Persistance des résumés de CORPUS MÉTIER dans le fil de discussion.

Un résumé demandé depuis le panneau de sources doit survivre au rechargement du
fil, exactement comme son équivalent privé — et porter sa PROVENANCE, sans quoi
relire un fil ancien ne permet plus de savoir quel document a été résumé.
"""
import pytest
from rest_framework.test import APIClient

import apps.corpus.services.summary as corpus_svc
from apps.chat.models import ChatMessage, ChatThread
from apps.chat.services.summary_messages import source_ref_of
from apps.corpus.models import (
    CorpusDocumentStatus,
    DomainCorpusChunk,
    DomainCorpusDocument,
)
from apps.rag_core.models import Document
from apps.rag_core.services.ingest_facade import ingest_plain_text
from apps.workspaces.models import Membership

pytestmark = pytest.mark.django_db


def _url(document_id) -> str:
    return f"/api/v1/domain-corpus-documents/{document_id}/summary/"


@pytest.fixture(autouse=True)
def _enable_feature(settings):
    settings.DOMAIN_CORPUS_SUMMARY_ENABLED = True
    return settings


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    monkeypatch.setattr(
        corpus_svc, "generate_text",
        lambda **kw: {"text": "RÉSUMÉ CORPUS", "provider": "ollama", "model": "m", "tokens": 5},
    )


@pytest.fixture
def business_domain(db):
    from apps.corpus.models import BusinessDomain

    return BusinessDomain.objects.create(slug="logistique", name="Logistique")


@pytest.fixture
def domain_corpus(db, business_domain):
    from apps.corpus.models import DomainCorpus

    return DomainCorpus.objects.create(domain=business_domain, slug="vc", name="VC Corpus")


@pytest.fixture
def corpus_doc(domain_corpus):
    doc = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, title="Rapport VC", original_filename="vc.pdf",
        status=CorpusDocumentStatus.READY,
    )
    DomainCorpusChunk.objects.create(
        corpus=domain_corpus, document=doc, chunk_index=0,
        text="Contenu de la page treize.", page_number=13,
    )
    return doc


@pytest.fixture
def consumer(db, user, workspace, rag_space, domain_corpus):
    from apps.corpus.models import WorkspaceDomainCorpusAccess

    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Membership.ROLE_OWNER}
    )
    WorkspaceDomainCorpusAccess.objects.create(workspace=workspace, corpus=domain_corpus)
    client = APIClient()
    client.force_authenticate(user=user)
    thread = ChatThread.objects.create(user=user, rag_space=rag_space)
    return {
        "user": user, "workspace": workspace, "rag_space": rag_space,
        "client": client, "thread": thread, "corpus": domain_corpus,
    }


def _post(consumer, doc, *, request_id="rq-1", **body):
    payload = {
        "rag_space_id": str(consumer["rag_space"].id),
        "thread_id": str(consumer["thread"].id),
        "request_id": request_id,
        **body,
    }
    return consumer["client"].post(_url(doc.id), payload, format="json")


# ---------------------------------------------------------------------------
# Persistance
# ---------------------------------------------------------------------------
def test_summary_creates_user_and_assistant_messages(consumer, corpus_doc):
    res = _post(consumer, corpus_doc, scope="page", page=13)

    assert res.status_code == 200
    msgs = list(
        ChatMessage.objects.filter(thread=consumer["thread"]).order_by("created_at")
    )
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "Résume la page 13 du document **Rapport VC**"
    assert msgs[1].content == "RÉSUMÉ CORPUS"
    assert msgs[1].status == ChatMessage.STATUS_COMPLETED


def test_persisted_message_carries_provenance(consumer, corpus_doc):
    _post(consumer, corpus_doc, scope="page", page=13)

    assistant = ChatMessage.objects.get(
        thread=consumer["thread"], role=ChatMessage.ROLE_ASSISTANT
    )
    ref = source_ref_of(assistant)
    assert ref["source_type"] == "domain_corpus"
    assert ref["document_id"] == str(corpus_doc.id)
    assert ref["corpus_id"] == str(consumer["corpus"].id)
    assert assistant.meta["message_type"] == "document_summary"
    assert assistant.meta["summary"]["scope"] == "page"


def test_thread_reload_still_shows_the_summary(consumer, corpus_doc):
    _post(consumer, corpus_doc)

    res = consumer["client"].get(
        f"/api/v1/chat/threads/{consumer['thread'].id}/messages/"
    )
    assert res.status_code == 200
    contents = [m["content"] for m in res.data]
    assert "RÉSUMÉ CORPUS" in contents


def test_same_request_id_is_idempotent(consumer, corpus_doc):
    _post(consumer, corpus_doc, request_id="rq-same")
    _post(consumer, corpus_doc, request_id="rq-same")

    assert ChatMessage.objects.filter(thread=consumer["thread"]).count() == 2


def test_request_id_reused_for_another_target_is_refused(consumer, corpus_doc):
    """Les deux espaces d'UUID sont disjoints : `source_type` entre dans la clé.

    Sans ce refus, la seconde demande recevrait le résumé du PREMIER document.
    """
    _post(consumer, corpus_doc, request_id="rq-shared")

    ingest_plain_text(
        rag_space_id=consumer["rag_space"].id, filename="prive.txt", text="Note interne."
    )
    private_doc = Document.objects.get(rag_space=consumer["rag_space"], filename="prive.txt")

    res = consumer["client"].post(
        f"/api/v1/documents/{private_doc.id}/summary/",
        {
            "thread_id": str(consumer["thread"].id),
            "request_id": "rq-shared",
        },
        format="json",
    )
    assert res.status_code == 409
    assert res.data["detail"] == "request_id_conflict"


def test_thread_without_request_id_is_rejected(consumer, corpus_doc):
    res = consumer["client"].post(
        _url(corpus_doc.id),
        {
            "rag_space_id": str(consumer["rag_space"].id),
            "thread_id": str(consumer["thread"].id),
        },
        format="json",
    )
    assert res.status_code == 400
    assert res.data["detail"] == "request_id_required"


def test_failure_before_exchange_persists_nothing(consumer, corpus_doc, settings):
    """Refus d'accès : aucun message ne doit rester dans le fil.

    C'est ce qui rend légitime le retrait du message utilisateur optimiste côté
    frontend — le serveur n'a effectivement rien écrit.
    """
    settings.DOMAIN_CORPUS_SUMMARY_ENABLED = False

    res = _post(consumer, corpus_doc)

    assert res.status_code == 404
    assert ChatMessage.objects.filter(thread=consumer["thread"]).count() == 0


def test_not_ready_document_marks_the_message_failed(consumer, corpus_doc):
    """L'échange existe déjà : il doit basculer en échec, pas rester « en cours »."""
    corpus_doc.status = CorpusDocumentStatus.INGESTING
    corpus_doc.save(update_fields=["status"])

    res = _post(consumer, corpus_doc)

    assert res.status_code == 409
    assistant = ChatMessage.objects.get(
        thread=consumer["thread"], role=ChatMessage.ROLE_ASSISTANT
    )
    assert assistant.status == ChatMessage.STATUS_FAILED
    # Aucun détail interne dans le contenu affiché.
    assert "document_not_ready" not in assistant.content


def test_private_summary_also_carries_provenance(consumer):
    """Non-régression : le chemin privé pose désormais la même provenance explicite."""
    ingest_plain_text(
        rag_space_id=consumer["rag_space"].id, filename="note.txt", text="Note interne."
    )
    private_doc = Document.objects.get(rag_space=consumer["rag_space"], filename="note.txt")

    import apps.rag_core.services.summary_service as private_svc

    def _fake(**kw):
        return {"text": "RÉSUMÉ PRIVÉ", "provider": "ollama", "model": "m", "tokens": 3}

    original = private_svc.generate_text
    private_svc.generate_text = _fake
    try:
        res = consumer["client"].post(
            f"/api/v1/documents/{private_doc.id}/summary/",
            {"thread_id": str(consumer["thread"].id), "request_id": "rq-priv"},
            format="json",
        )
    finally:
        private_svc.generate_text = original

    assert res.status_code == 200
    assistant = ChatMessage.objects.get(
        thread=consumer["thread"], role=ChatMessage.ROLE_ASSISTANT
    )
    assert source_ref_of(assistant)["source_type"] == "workspace_document"


def test_legacy_message_without_meta_reads_as_private(consumer):
    """Compatibilité historique : `meta` absent => document privé, pas « inconnu ».

    À l'époque de ces messages, le résumé n'existait que pour le privé : la valeur
    est exacte, pas devinée.
    """
    legacy = ChatMessage.objects.create(
        thread=consumer["thread"], role=ChatMessage.ROLE_ASSISTANT,
        content="Ancien résumé", kind=ChatMessage.KIND_DOCUMENT_SUMMARY,
        status=ChatMessage.STATUS_COMPLETED,
        document_id="11111111-1111-1111-1111-111111111111",
    )

    ref = source_ref_of(legacy)
    assert ref["source_type"] == "workspace_document"
    assert ref["document_id"] == "11111111-1111-1111-1111-111111111111"
