"""Tests de l'API de résumé des documents de CORPUS MÉTIER.

Couvre l'endpoint dédié ``/api/v1/domain-corpus-documents/<id>/summary/`` :
accès légitime (document + page), permissions par association workspace ↔ corpus,
anti-énumération, statuts documentaires, cache et invariants de sécurité — dont
la séparation stricte des deux espaces d'identifiants.

Le LLM est toujours stubé : aucun appel réseau réel.
"""
import uuid

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

import apps.corpus.services.summary as corpus_svc
from apps.corpus.models import (
    CorpusDocumentStatus,
    DomainCorpusChunk,
    DomainCorpusDocument,
    DomainCorpusDocumentSummary,
)
from apps.rag_core.models import Document, RagSpace
from apps.rag_core.services.ingest_facade import ingest_plain_text
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db


def _url(document_id) -> str:
    return f"/api/v1/domain-corpus-documents/{document_id}/summary/"


class _FakeLLM:
    """Stub capturant les kwargs passés à generate_text."""

    def __init__(self, *, text="RÉSUMÉ CORPUS", provider="ollama", model="qwen-stub", tokens=17):
        self.calls = []
        self._text = text
        self._provider = provider
        self._model = model
        self._tokens = tokens

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "text": self._text,
            "tokens": self._tokens,
            "cost": 0.0,
            "provider": self._provider,
            "model": self._model,
            "usage": None,
            "raw": None,
        }


@pytest.fixture(autouse=True)
def _enable_feature(settings):
    """La fonctionnalité est OFF par défaut : ces tests l'activent explicitement.

    Un test dédié vérifie le comportement flag OFF — il l'éteint lui-même.
    """
    settings.DOMAIN_CORPUS_SUMMARY_ENABLED = True
    return settings


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    llm = _FakeLLM()
    monkeypatch.setattr(corpus_svc, "generate_text", llm)
    return llm


# Fixtures corpus déclarées LOCALEMENT : celles de `tests_root/corpus/conftest.py`
# ne portent pas jusqu'à cet arbre, et les ajouter au conftest partagé de `http_api`
# les imposerait à 400 tests qui n'en ont pas besoin.
@pytest.fixture
def business_domain(db):
    from apps.corpus.models import BusinessDomain

    return BusinessDomain.objects.create(slug="logistique", name="Logistique")


@pytest.fixture
def domain_corpus(db, business_domain):
    from apps.corpus.models import DomainCorpus

    return DomainCorpus.objects.create(
        domain=business_domain,
        slug="vc",
        name="VC Reference Corpus",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )


@pytest.fixture
def grant_access(db):
    from apps.corpus.models import WorkspaceDomainCorpusAccess

    def _grant(*, workspace, corpus, enabled=True):
        access, _ = WorkspaceDomainCorpusAccess.objects.update_or_create(
            workspace=workspace, corpus=corpus, defaults={"enabled": enabled}
        )
        return access

    return _grant


@pytest.fixture
def corpus_doc(domain_corpus):
    """Document de corpus PRÊT, 2 pages, plusieurs extraits par page."""
    doc = DomainCorpusDocument.objects.create(
        corpus=domain_corpus,
        title="Venture Capital in Europe",
        original_filename="jrc_vc_europe.pdf",
        status=CorpusDocumentStatus.READY,
    )
    for idx, (page, text) in enumerate(
        [
            (12, "Page douze : contexte général du financement."),
            (13, "Page treize, premier extrait : les fonds d'amorçage."),
            (13, "Page treize, second extrait : les tickets moyens."),
            (14, "Page quatorze : conclusions."),
        ]
    ):
        DomainCorpusChunk.objects.create(
            corpus=domain_corpus, document=doc, chunk_index=idx, text=text, page_number=page
        )
    return doc


@pytest.fixture
def consumer(db, user, workspace, rag_space, domain_corpus, grant_access):
    """Workspace consommateur, membre, et accès ACTIF au corpus."""
    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Membership.ROLE_OWNER}
    )
    grant_access(workspace=workspace, corpus=domain_corpus)
    client = APIClient()
    client.force_authenticate(user=user)
    return {
        "user": user, "workspace": workspace, "rag_space": rag_space,
        "client": client, "corpus": domain_corpus,
    }


def _post(consumer, doc, **body):
    payload = {"rag_space_id": str(consumer["rag_space"].id), **body}
    return consumer["client"].post(_url(doc.id), payload, format="json")


# ---------------------------------------------------------------------------
# §23 — Accès autorisé
# ---------------------------------------------------------------------------
def test_summary_of_corpus_document_returns_200(consumer, corpus_doc):
    res = _post(consumer, corpus_doc)

    assert res.status_code == 200
    assert res.data["status"] == "ready"
    assert res.data["summary"] == "RÉSUMÉ CORPUS"
    assert res.data["source_type"] == "domain_corpus"
    assert res.data["document_id"] == str(corpus_doc.id)
    assert res.data["corpus_id"] == str(consumer["corpus"].id)
    assert res.data["filename"] == "Venture Capital in Europe"


def test_page_summary_uses_only_that_page(consumer, corpus_doc, _stub_llm):
    res = _post(consumer, corpus_doc, scope="page", page=13)

    assert res.status_code == 200
    assert res.data["scope"] == "page"
    assert res.data["page"] == 13

    prompt = _stub_llm.calls[0]["prompt"]
    assert "premier extrait" in prompt
    assert "second extrait" in prompt
    # Aucune autre page ne doit entrer dans le contexte — c'est l'invariant
    # central du scope page (jamais de bascule silencieuse).
    assert "Page douze" not in prompt
    assert "Page quatorze" not in prompt


def test_page_summary_preserves_chunk_order(consumer, corpus_doc, _stub_llm):
    _post(consumer, corpus_doc, scope="page", page=13)

    prompt = _stub_llm.calls[0]["prompt"]
    assert prompt.index("premier extrait") < prompt.index("second extrait")


def test_document_summary_includes_all_pages(consumer, corpus_doc, _stub_llm):
    _post(consumer, corpus_doc)

    prompt = _stub_llm.calls[0]["prompt"]
    for marker in ("Page douze", "premier extrait", "second extrait", "Page quatorze"):
        assert marker in prompt


def test_summary_uses_consumer_space_provider(consumer, corpus_doc, _stub_llm):
    """La politique LLM vient de l'espace CONSOMMATEUR, pas du corpus."""
    space = consumer["rag_space"]
    space.llm_provider = "ollama"
    space.llm_model = "llama3.1:8b"
    space.save(update_fields=["llm_provider", "llm_model"])

    _post(consumer, corpus_doc)

    call = _stub_llm.calls[0]
    assert call["provider"] == "ollama"
    assert call["model"] == "llama3.1:8b"
    # Le corpus déclare `openai` pour ses EMBEDDINGS : cela ne doit jamais fuiter
    # vers la génération.
    assert consumer["corpus"].embedding_provider == "openai"


def test_summary_persists_cache_row(consumer, corpus_doc):
    _post(consumer, corpus_doc)

    row = DomainCorpusDocumentSummary.objects.get(document=corpus_doc)
    assert row.status == "READY"
    assert row.rag_space_id == consumer["rag_space"].id
    assert row.workspace_id == consumer["workspace"].id
    assert row.corpus_id == consumer["corpus"].id
    assert row.source_fingerprint


# ---------------------------------------------------------------------------
# §24 — Permissions
# ---------------------------------------------------------------------------
def test_member_without_corpus_access_gets_404(db, user, workspace, rag_space, corpus_doc):
    """Membre du workspace, mais AUCUNE association au corpus."""
    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Membership.ROLE_OWNER}
    )
    client = APIClient()
    client.force_authenticate(user=user)

    res = client.post(
        _url(corpus_doc.id), {"rag_space_id": str(rag_space.id)}, format="json"
    )
    assert res.status_code == 404
    assert res.data["detail"] == "not_found"


def test_disabled_association_gets_404(consumer, corpus_doc, grant_access):
    grant_access(workspace=consumer["workspace"], corpus=consumer["corpus"], enabled=False)

    res = _post(consumer, corpus_doc)
    assert res.status_code == 404


def test_inactive_corpus_gets_404(consumer, corpus_doc):
    corpus = consumer["corpus"]
    corpus.is_active = False
    corpus.save(update_fields=["is_active"])

    res = _post(consumer, corpus_doc)
    assert res.status_code == 404


def test_foreign_workspace_cannot_summarize(foreign_setup, corpus_doc):
    """Un utilisateur d'un AUTRE workspace, même si CE corpus est associé ailleurs."""
    res = foreign_setup["client"].post(
        _url(corpus_doc.id),
        {"rag_space_id": str(foreign_setup["rag_space"].id)},
        format="json",
    )
    assert res.status_code == 404


def test_viewer_can_summarize(db, viewer_setup, domain_corpus, corpus_doc, grant_access):
    """Le résumé est une LECTURE : un viewer y a droit, comme sur le chemin privé."""
    grant_access(workspace=viewer_setup["workspace"], corpus=domain_corpus)

    res = viewer_setup["client"].post(
        _url(corpus_doc.id),
        {"rag_space_id": str(viewer_setup["rag_space"].id)},
        format="json",
    )
    assert res.status_code == 200


def test_unknown_uuid_and_forbidden_are_indistinguishable(consumer, corpus_doc, grant_access):
    """Anti-énumération : inexistant et interdit doivent produire la même réponse."""
    missing = consumer["client"].post(
        _url(uuid.uuid4()), {"rag_space_id": str(consumer["rag_space"].id)}, format="json"
    )
    grant_access(workspace=consumer["workspace"], corpus=consumer["corpus"], enabled=False)
    forbidden = _post(consumer, corpus_doc)

    assert missing.status_code == forbidden.status_code == 404
    assert missing.data == forbidden.data


def test_document_of_another_corpus_not_reachable(consumer, business_domain, grant_access):
    """Un corpus non associé reste invisible, même pour un workspace habilité ailleurs."""
    from apps.corpus.models import DomainCorpus

    other_corpus = DomainCorpus.objects.create(
        domain=business_domain, slug="autre", name="Autre corpus"
    )
    other_doc = DomainCorpusDocument.objects.create(
        corpus=other_corpus, original_filename="secret.pdf", status=CorpusDocumentStatus.READY
    )
    DomainCorpusChunk.objects.create(
        corpus=other_corpus, document=other_doc, chunk_index=0, text="Confidentiel."
    )

    res = _post(consumer, other_doc)
    assert res.status_code == 404


def test_feature_flag_off_is_indistinguishable_from_missing(consumer, corpus_doc, settings):
    settings.DOMAIN_CORPUS_SUMMARY_ENABLED = False

    res = _post(consumer, corpus_doc)
    assert res.status_code == 404
    assert res.data["detail"] == "not_found"


# ---------------------------------------------------------------------------
# §25 — Statuts documentaires et validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_status",
    [CorpusDocumentStatus.PENDING, CorpusDocumentStatus.INGESTING, CorpusDocumentStatus.FAILED],
)
def test_document_not_ready_returns_409(consumer, corpus_doc, bad_status):
    corpus_doc.status = bad_status
    corpus_doc.save(update_fields=["status"])

    res = _post(consumer, corpus_doc)
    assert res.status_code == 409
    assert res.data["detail"] == "document_not_ready"


def test_document_without_chunks_returns_409(consumer, domain_corpus):
    empty = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="vide.pdf", status=CorpusDocumentStatus.READY
    )

    res = _post(consumer, empty)
    assert res.status_code == 409
    assert res.data["detail"] == "document_no_content"


def test_page_without_chunks_returns_409(consumer, corpus_doc):
    res = _post(consumer, corpus_doc, scope="page", page=999)
    assert res.status_code == 409
    assert res.data["detail"] == "document_no_content"


def test_blank_chunks_are_excluded(consumer, domain_corpus):
    doc = DomainCorpusDocument.objects.create(
        corpus=domain_corpus, original_filename="blancs.pdf", status=CorpusDocumentStatus.READY
    )
    DomainCorpusChunk.objects.create(
        corpus=domain_corpus, document=doc, chunk_index=0, text="   ", page_number=1
    )

    res = _post(consumer, doc)
    assert res.status_code == 409
    assert res.data["detail"] == "document_no_content"


def test_invalid_scope_falls_back_to_document(consumer, corpus_doc):
    """Scope inconnu : normalisé vers `document`, jamais rejeté silencieusement."""
    res = _post(consumer, corpus_doc, scope="chapitre")
    assert res.status_code == 200
    assert res.data["scope"] == "document"


def test_page_scope_without_page_falls_back_to_document(consumer, corpus_doc):
    res = _post(consumer, corpus_doc, scope="page")
    assert res.status_code == 200
    assert res.data["scope"] == "document"


def test_invalid_mode_falls_back_to_default(consumer, corpus_doc):
    res = _post(consumer, corpus_doc, mode="haiku")
    assert res.status_code == 200
    assert res.data["mode"] == "bullets"


def test_missing_consumer_space_returns_404(consumer, corpus_doc):
    """Sans espace consommateur, aucune politique LLM n'est résolvable."""
    res = consumer["client"].post(_url(corpus_doc.id), {}, format="json")
    assert res.status_code == 404


def test_unknown_rag_space_returns_404(consumer, corpus_doc):
    res = consumer["client"].post(
        _url(corpus_doc.id), {"rag_space_id": str(uuid.uuid4())}, format="json"
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# §26 — Sécurité : séparation des espaces d'identifiants
# ---------------------------------------------------------------------------
def test_corpus_document_rejected_by_private_endpoint(consumer, corpus_doc):
    """Un identifiant de corpus reste inconnu de l'API privée. C'était LE défaut."""
    res = consumer["client"].post(
        f"/api/v1/documents/{corpus_doc.id}/summary/", {}, format="json"
    )
    assert res.status_code == 404
    assert res.data["detail"] == "not_found"


def test_private_document_rejected_by_corpus_endpoint(consumer):
    """Symétrique : l'endpoint corpus ne résout jamais un document privé."""
    ingest_plain_text(
        rag_space_id=consumer["rag_space"].id, filename="prive.txt", text="Note interne."
    )
    private_doc = Document.objects.get(rag_space=consumer["rag_space"], filename="prive.txt")

    res = _post(consumer, private_doc)
    assert res.status_code == 404


def test_thread_of_another_workspace_is_refused(consumer, corpus_doc, other_user):
    """Un thread_id volé à un autre workspace ne doit rien pouvoir déclencher."""
    from apps.chat.models import ChatThread

    other_ws = Workspace.objects.create(name="WOther")
    Membership.objects.create(workspace=other_ws, user=other_user, role=Membership.ROLE_OWNER)
    other_space = RagSpace.objects.create(name="other", workspace=other_ws)
    foreign_thread = ChatThread.objects.create(user=other_user, rag_space=other_space)

    res = consumer["client"].post(
        _url(corpus_doc.id),
        {
            "rag_space_id": str(consumer["rag_space"].id),
            "thread_id": str(foreign_thread.id),
            "request_id": "rq-1",
        },
        format="json",
    )
    assert res.status_code == 404


def test_thread_and_rag_space_must_agree(consumer, corpus_doc):
    """Fil d'un espace + rag_space_id d'un autre : refus, pas d'arbitrage implicite."""
    from apps.chat.models import ChatThread

    second_space = RagSpace.objects.create(name="second", workspace=consumer["workspace"])
    thread = ChatThread.objects.create(user=consumer["user"], rag_space=second_space)

    res = consumer["client"].post(
        _url(corpus_doc.id),
        {
            "rag_space_id": str(consumer["rag_space"].id),
            "thread_id": str(thread.id),
            "request_id": "rq-mismatch",
        },
        format="json",
    )
    assert res.status_code == 404


def test_workspace_is_never_taken_from_payload(consumer, corpus_doc, foreign_setup):
    """Un `workspace_id` fourni par le client n'a aucun effet : il est ignoré."""
    grant_res = _post(consumer, corpus_doc, workspace_id=foreign_setup["workspace"].id)
    assert grant_res.status_code == 200

    row = DomainCorpusDocumentSummary.objects.get(document=corpus_doc)
    # Le workspace enregistré est celui DÉRIVÉ du RagSpace authentifié.
    assert row.workspace_id == consumer["workspace"].id


def test_anonymous_is_rejected(corpus_doc):
    res = APIClient().post(_url(corpus_doc.id), {}, format="json")
    assert res.status_code in (401, 403)


def test_error_response_carries_no_document_content(consumer, corpus_doc, grant_access):
    grant_access(workspace=consumer["workspace"], corpus=consumer["corpus"], enabled=False)

    res = _post(consumer, corpus_doc)
    body = str(res.data)
    assert "Page treize" not in body
    assert "jrc_vc_europe" not in body
    assert "Venture Capital" not in body


# ---------------------------------------------------------------------------
# §28 — Cache et invalidation
# ---------------------------------------------------------------------------
def test_second_call_hits_cache_without_llm(consumer, corpus_doc, _stub_llm):
    first = _post(consumer, corpus_doc)
    second = _post(consumer, corpus_doc)

    assert first.status_code == second.status_code == 200
    assert second.data["cached"] is True
    assert len(_stub_llm.calls) == 1


def test_refresh_bypasses_cache(consumer, corpus_doc, _stub_llm):
    _post(consumer, corpus_doc)
    res = _post(consumer, corpus_doc, refresh=True)

    assert res.status_code == 200
    assert res.data["cached"] is False
    assert len(_stub_llm.calls) == 2
    assert DomainCorpusDocumentSummary.objects.get(document=corpus_doc).refresh_revision == 1


def test_cache_invalidated_when_chunks_change(consumer, corpus_doc, domain_corpus, _stub_llm):
    _post(consumer, corpus_doc)
    DomainCorpusChunk.objects.create(
        corpus=domain_corpus, document=corpus_doc, chunk_index=9,
        text="Nouvel extrait après réindexation.", page_number=15,
    )

    res = _post(consumer, corpus_doc)
    assert res.data["cached"] is False
    assert len(_stub_llm.calls) == 2


def test_scope_page_and_mode_are_cached_separately(consumer, corpus_doc, _stub_llm):
    _post(consumer, corpus_doc)
    _post(consumer, corpus_doc, scope="page", page=13)
    _post(consumer, corpus_doc, mode="short")

    assert len(_stub_llm.calls) == 3
    assert DomainCorpusDocumentSummary.objects.filter(document=corpus_doc).count() == 3


@override_settings(RAG_ANSWER_LANGUAGE_POLICY_ENABLED=True)
def test_language_variants_are_cached_separately(consumer, corpus_doc, _stub_llm):
    """Le drapeau est posé ICI, et non hérité de l'environnement.

    `RAG_ANSWER_LANGUAGE_POLICY_ENABLED` vaut `False` par défaut au registre. Drapeau
    éteint, `resolve_target` rend `None` et la langue effective est TOUJOURS `fr` :
    les deux demandes partagent alors la même `variant_key`, la seconde est servie du
    cache, et le test échoue avec un seul appel LLM. Il ne passait donc que sur un
    poste dont le `.env` active la politique — pas en CI.
    """
    _post(consumer, corpus_doc, language="fr")
    _post(consumer, corpus_doc, language="en")

    assert len(_stub_llm.calls) == 2
    assert DomainCorpusDocumentSummary.objects.filter(document=corpus_doc).count() == 2


def test_two_workspaces_do_not_share_the_cache(consumer, corpus_doc, domain_corpus,
                                               other_user, grant_access, _stub_llm):
    """Deux espaces consommateurs = deux politiques d'exécution possibles.

    Servir à l'un le résumé produit sous la configuration de l'autre contournerait
    ses réglages, et potentiellement sa politique d'egress.
    """
    _post(consumer, corpus_doc)

    ws2 = Workspace.objects.create(name="WS2")
    Membership.objects.create(workspace=ws2, user=other_user, role=Membership.ROLE_OWNER)
    space2 = RagSpace.objects.create(name="space2", workspace=ws2)
    grant_access(workspace=ws2, corpus=domain_corpus)
    client2 = APIClient()
    client2.force_authenticate(user=other_user)

    res = client2.post(_url(corpus_doc.id), {"rag_space_id": str(space2.id)}, format="json")

    assert res.status_code == 200
    assert res.data["cached"] is False
    assert len(_stub_llm.calls) == 2
    assert DomainCorpusDocumentSummary.objects.filter(document=corpus_doc).count() == 2


def test_access_revocation_blocks_even_with_existing_cache(consumer, corpus_doc, grant_access):
    """Le contrôle d'accès précède TOUJOURS la restitution du cache."""
    first = _post(consumer, corpus_doc)
    assert first.status_code == 200
    assert DomainCorpusDocumentSummary.objects.filter(document=corpus_doc).exists()

    grant_access(workspace=consumer["workspace"], corpus=consumer["corpus"], enabled=False)

    res = _post(consumer, corpus_doc)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# GET (polling)
# ---------------------------------------------------------------------------
def test_get_returns_none_when_absent(consumer, corpus_doc):
    res = consumer["client"].get(
        _url(corpus_doc.id), {"rag_space_id": str(consumer["rag_space"].id)}
    )
    assert res.status_code == 200
    assert res.data["status"] == "none"


def test_get_returns_cached_summary(consumer, corpus_doc):
    _post(consumer, corpus_doc)

    res = consumer["client"].get(
        _url(corpus_doc.id), {"rag_space_id": str(consumer["rag_space"].id)}
    )
    assert res.status_code == 200
    assert res.data["status"] == "ready"
    assert res.data["summary"] == "RÉSUMÉ CORPUS"


def test_get_is_refused_after_access_revocation(consumer, corpus_doc, grant_access):
    _post(consumer, corpus_doc)
    grant_access(workspace=consumer["workspace"], corpus=consumer["corpus"], enabled=False)

    res = consumer["client"].get(
        _url(corpus_doc.id), {"rag_space_id": str(consumer["rag_space"].id)}
    )
    assert res.status_code == 404
