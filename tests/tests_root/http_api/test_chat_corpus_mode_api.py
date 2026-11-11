"""L3 — clarification « rapide / complet » : API sync, SSE, no-AI, persistance.

Preuves : la clarification et le mode complet indisponible ne touchent NI retrieval,
NI embeddings, NI LLM, NI rerank, NI Celery, NI RagQueryLog — avec contrôle négatif.
"""
import importlib
import json

import pytest
from rest_framework.test import APIClient

from apps.audit.models import RagQueryLog
from apps.chat.models import ChatMessage, ChatThread
from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus

pytestmark = pytest.mark.django_db

GLOBAL_Q = "résume tous les documents"


def _doc(space, *, status=DocumentStatus.READY, name="d.txt", sha="a"):
    return Document.objects.create(rag_space=space, filename=name, status=status, sha256=sha)


def _chunk(space, doc, *, text="Le contrat court sur douze mois.", page=1, idx=0):
    return Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=idx, text=text, page_number=page
    )


@pytest.fixture
def corpus(rag_space):
    """3 documents exploitables + 1 en échec (donc couverture jamais complète)."""
    for i in range(3):
        d = _doc(rag_space, name=f"ok{i}.txt", sha=f"s{i}")
        _chunk(rag_space, d, idx=i)
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")
    return rag_space


def _post(client, space, question, thread_id=None, request_id=None):
    body = {"rag_space_id": str(space.id), "question": question}
    if thread_id:
        body["thread_id"] = str(thread_id)
    headers = {"HTTP_X_REQUEST_ID": request_id} if request_id else {}
    return client.post("/api/v1/chat/", body, format="json", **headers)


def _stream(client, space, question, thread_id=None):
    body = {"rag_space_id": str(space.id), "question": question}
    if thread_id:
        body["thread_id"] = str(thread_id)
    res = client.post("/api/v1/chat/stream/", body, format="json")
    return b"".join(res.streaming_content).decode("utf-8")


def _sse_events(raw: str):
    out = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


# --------------------------------------------------------------------------- #
# Clarification — sync
# --------------------------------------------------------------------------- #

def test_global_request_returns_mode_choice(api_client, corpus):
    res = _post(api_client, corpus, GLOBAL_Q)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_mode_choice"
    assert res.data["sources"] == []
    assert res.data["log_id"] is None

    answer = res.data["answer"]
    assert "Cette demande porte sur l’ensemble du corpus" in answer
    # Chiffres RÉELS du RagSpace (4 documents, 3 exploitables, 1 non analysable).
    assert "4 documents considérés" in answer
    assert "3 documents exploitables" in answer
    assert "1 documents non analysables" in answer or "1 document" in answer
    # Les deux modes sont annoncés, et le mode complet est déclaré indisponible.
    assert "Réponse rapide" in answer
    assert "couverture partielle" in answer
    assert "Analyse complète" in answer
    assert "n’est pas encore disponible" in answer


def test_mode_choice_carries_real_coverage(api_client, corpus):
    cov = _post(api_client, corpus, GLOBAL_Q).data["coverage"]

    assert cov["documents_considered"] == 4
    assert cov["documents_analyzed"] == 3
    assert cov["coverage_complete"] is False
    assert cov["coverage_level"] in ("A", "B")
    assert any(e["reason"] == "extraction_failed" for e in cov["documents_excluded"])


def test_mode_choice_never_promises_exhaustivity(api_client, corpus):
    answer = _post(api_client, corpus, GLOBAL_Q).data["answer"].lower()
    for banned in (
        "analyse exhaustive disponible",
        "tous les documents seront analysés",
        "intégralité garantie",
        "couverture sémantique complète",
    ):
        assert banned not in answer


def test_empty_space_says_so(api_client, rag_space):
    res = _post(api_client, rag_space, GLOBAL_Q)
    assert res.status_code == 200
    assert "aucun document" in res.data["answer"].lower()


def test_targeted_question_is_untouched(api_client, corpus, monkeypatch):
    """Non-régression : une question ciblée reste une question ciblée."""
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
        "status": "ok", "sources": [], "prompt": "P", "log_id": None, "answer_kind": "ok"})
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "Douze mois.", "provider": "stub", "model": "m", "usage": {}})
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

    res = _post(api_client, corpus, "quelle est la durée du contrat ?")
    assert res.status_code == 200
    assert res.data.get("answer_kind") not in ("corpus_mode_choice", "corpus_quick_answer")
    # Aucun avertissement de couverture partielle sur une question ciblée.
    assert not (res.data.get("answer") or "").startswith("> **Couverture partielle**")


# --------------------------------------------------------------------------- #
# Clarification — streaming (contrat SSE inchangé)
# --------------------------------------------------------------------------- #

def test_mode_choice_streaming_contract(api_client, corpus):
    events = _sse_events(_stream(api_client, corpus, GLOBAL_Q))
    names = [n for n, _ in events]

    assert names == ["meta", "sources", "token", "done"]  # aucun nouvel événement

    data = dict(events)
    assert data["meta"]["answer_kind"] == "corpus_mode_choice"
    assert data["meta"]["log_id"] is None
    assert data["sources"]["sources"] == []
    assert "Cette demande porte sur l’ensemble du corpus" in data["token"]["delta"]
    assert data["done"]["answer_kind"] == "corpus_mode_choice"
    assert data["done"]["log_id"] is None
    assert data["done"]["status"] == "ok"
    assert "error" not in names


# --------------------------------------------------------------------------- #
# Mode complet — indisponible, aucun traitement
# --------------------------------------------------------------------------- #

def test_full_analysis_is_honestly_unavailable(api_client, corpus):
    thread_id = _post(api_client, corpus, GLOBAL_Q).data["thread_id"]
    res = _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_full_unavailable"
    assert res.data["log_id"] is None
    assert res.data["sources"] == []
    assert "n’est pas encore disponible" in res.data["answer"]
    assert "réponse rapide" in res.data["answer"]


# --------------------------------------------------------------------------- #
# Mode rapide
# --------------------------------------------------------------------------- #

def test_quick_confirm_replays_the_original_question(api_client, corpus, monkeypatch):
    """R1 — la question d'origine est reprise, l'utilisateur ne la retape pas."""
    seen = {}

    def fake_query_rag(**kw):
        seen["question"] = kw.get("question")
        return {
            "status": "ok",
            "sources": [{"chunk_id": "c1", "document_id": "d1", "text": "x"}],
            "prompt": "P",
            "log_id": None,
            "answer_kind": "ok",
        }

    def fake_generate_text(**kw):
        return {"text": "Le contrat dure douze mois.", "provider": "stub", "model": "m", "usage": {}}

    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", fake_query_rag)
    monkeypatch.setattr(cs, "generate_text", fake_generate_text)
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

    thread_id = _post(api_client, corpus, GLOBAL_Q).data["thread_id"]
    res = _post(api_client, corpus, "réponse rapide", thread_id=thread_id)

    assert res.status_code == 200
    # Le RAG a bien reçu la question D'ORIGINE, pas « réponse rapide ».
    assert seen["question"] == GLOBAL_Q
    assert res.data["answer_kind"] == "corpus_quick_answer"


def test_quick_answer_always_carries_the_warning(api_client, corpus, monkeypatch):
    """R4 — même si le LLM affirme le contraire, l'avertissement est imposé."""
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
        "status": "ok", "sources": [{"chunk_id": "c1", "document_id": "d1"}],
        "prompt": "P", "log_id": None, "answer_kind": "ok",
    })
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "J’ai analysé tous les documents de la base, de façon exhaustive.",
        "provider": "stub", "model": "m", "usage": {},
    })
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

    thread_id = _post(api_client, corpus, GLOBAL_Q).data["thread_id"]
    answer = _post(api_client, corpus, "réponse rapide", thread_id=thread_id).data["answer"]

    assert answer.startswith("> **Couverture partielle**")
    assert "ne constitue pas un parcours complet du corpus" in answer
    # Le mensonge du LLM subsiste dans son texte, mais il est encadré par le backend.
    assert "exhaustive" in answer.lower()


def test_quick_answer_coverage_is_built_on_the_retrieved_sources(api_client, corpus, monkeypatch):
    """Le point non négociable : « READY avec extraits » ≠ « analysé pour cette requête ».

    L'espace a 3 documents exploitables ; le retrieval n'en a retenu qu'UN.
    La couverture doit dire 1, jamais 3.
    """
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
        "status": "ok",
        "sources": [
            {"chunk_id": "c1", "document_id": "doc-A"},
            {"chunk_id": "c2", "document_id": "doc-A"},  # même document
        ],
        "prompt": "P", "log_id": None, "answer_kind": "ok",
    })
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "Réponse.", "provider": "stub", "model": "m", "usage": {}})
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

    thread_id = _post(api_client, corpus, GLOBAL_Q).data["thread_id"]
    cov = _post(api_client, corpus, "réponse rapide", thread_id=thread_id).data["coverage"]

    assert cov["documents_considered"] == 4
    assert cov["documents_analyzed"] == 1        # UN document, pas 3
    assert cov["chunks_analyzed"] == 2
    assert cov["coverage_complete"] is False
    assert cov["limits_applied"] == ["top_k"]


def test_quick_confirm_without_context_is_an_ordinary_question(api_client, corpus, monkeypatch):
    """R2 — « réponse rapide » seul ne réutilise AUCUNE ancienne question."""
    seen = {}
    import apps.chat.services.chat_service as cs

    def fake_query_rag(**kw):
        seen["question"] = kw.get("question")
        return {"status": "ok", "sources": [], "prompt": "P", "log_id": None, "answer_kind": "ok"}

    monkeypatch.setattr(cs, "_dispatch_retrieval", fake_query_rag)
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "…", "provider": "stub", "model": "m", "usage": {}})
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

    res = _post(api_client, corpus, "réponse rapide")  # aucun thread, aucune clarification

    assert res.status_code == 200
    assert res.data.get("answer_kind") != "corpus_quick_answer"
    # La question traitée est bien celle tapée, pas une question ressuscitée.
    assert seen.get("question") == "réponse rapide"


# --------------------------------------------------------------------------- #
# Persistance et idempotence
# --------------------------------------------------------------------------- #

def test_clarification_persists_user_and_assistant(api_client, corpus):
    thread_id = _post(api_client, corpus, GLOBAL_Q).data["thread_id"]
    msgs = list(ChatMessage.objects.filter(thread_id=thread_id).order_by("created_at"))

    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == GLOBAL_Q
    assert msgs[1].content.startswith("## Cette demande porte")
    assert msgs[1].sources == []
    assert msgs[1].rag_query_log_id is None


def test_clarification_is_idempotent_on_request_id(api_client, corpus):
    """R3 — même thread_id + même request_id -> pas de doublon, pas de recalcul."""
    first = _post(api_client, corpus, GLOBAL_Q, request_id="rid-1")
    thread_id = first.data["thread_id"]

    _post(api_client, corpus, GLOBAL_Q, thread_id=thread_id, request_id="rid-1")

    assert ChatMessage.objects.filter(thread_id=thread_id, request_id="rid-1").count() == 2
    assert ChatThread.objects.filter(id=thread_id).count() == 1


def test_full_thread_shows_the_conversation(api_client, corpus, monkeypatch):
    """Le fil doit être relisible : question globale, choix, confirmation, réponse."""
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
        "status": "ok", "sources": [{"chunk_id": "c1", "document_id": "d1"}],
        "prompt": "P", "log_id": None, "answer_kind": "ok"})
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "Réponse.", "provider": "stub", "model": "m", "usage": {}})
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

    thread_id = _post(api_client, corpus, GLOBAL_Q).data["thread_id"]
    _post(api_client, corpus, "réponse rapide", thread_id=thread_id)

    msgs = list(ChatMessage.objects.filter(thread_id=thread_id).order_by("created_at"))
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0].content == GLOBAL_Q                      # question d'origine
    assert msgs[1].content.startswith("## Cette demande")   # clarification
    assert msgs[2].content == "réponse rapide"              # ce que l'utilisateur a tapé
    assert msgs[3].content.startswith("> **Couverture partielle**")  # avertissement PERSISTÉ


# --------------------------------------------------------------------------- #
# Preuves no-retrieval / no-embedding / no-LLM / no-rerank / no-Celery
# --------------------------------------------------------------------------- #

class ForbiddenAIPath(AssertionError):
    pass


@pytest.fixture
def no_ai(monkeypatch):
    def boom(*a, **kw):
        raise ForbiddenAIPath("FORBIDDEN_AI_PATH")

    targets = [
        ("apps.chat.services.chat_service", "query_rag"),
        ("apps.chat.services.chat_service", "_run_retrieval"),
        ("apps.chat.services.chat_service", "generate_text"),
        ("apps.chat.services.chat_service", "generate_text_stream"),
        ("apps.chat.services.chat_service", "llm_route_news_or_none"),
        ("apps.chat.services.chat_service", "enqueue_no_answer_suggestions"),
        ("apps.rag_core.services.query.orchestrator", "query_rag"),
        ("apps.rag_core.services.query.orchestrator", "embed_texts"),
        ("apps.llm_router.embeddings", "embed_texts"),
        ("apps.llm_router.router", "generate_text"),
        ("apps.llm_router.router", "generate_text_stream"),
        ("apps.rag_core.services.rerank", "rerank_chunks_observed"),
        ("apps.chat.intents.news", "llm_route_news_or_none"),
        ("apps.chat.intents.news", "summarize_news_with_llm"),
    ]
    for mod_path, attr in targets:
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            continue
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, boom, raising=True)

    from celery.app.task import Task

    monkeypatch.setattr(Task, "delay", boom, raising=True)
    monkeypatch.setattr(Task, "apply_async", boom, raising=True)
    return boom


@pytest.mark.parametrize("question", [
    "analyse tous les documents",
    "fais une synthèse de l'ensemble du corpus",
    "scan all documents",
    "ne te limite pas aux sources les plus pertinentes",
])
def test_clarification_never_touches_ai_paths(api_client, corpus, no_ai, question):
    res = _post(api_client, corpus, question)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_mode_choice"
    assert res.data["sources"] == []
    assert res.data["log_id"] is None


def test_clarification_never_touches_ai_paths_streaming(api_client, corpus, no_ai):
    raw = _stream(api_client, corpus, GLOBAL_Q)
    assert "FORBIDDEN_AI_PATH" not in raw
    assert [n for n, _ in _sse_events(raw)] == ["meta", "sources", "token", "done"]


def test_full_analysis_never_touches_ai_paths(api_client, corpus, no_ai):
    thread_id = _post(api_client, corpus, GLOBAL_Q).data["thread_id"]
    res = _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_full_unavailable"
    assert res.data["log_id"] is None


def test_control_a_targeted_question_would_hit_the_ai_path(api_client, corpus, no_ai):
    """CONTRÔLE NÉGATIF — sans lui, les preuves d'absence pourraient passer pour une
    mauvaise raison (patches mal posés).

    Une question ciblée DOIT atteindre un chemin IA. La vue attrape l'exception et
    renvoie 500 : c'est ce 500 qui prouve que les patches sont bien placés, et donc
    que les 200 des tests ci-dessus signifient réellement « aucun appel IA »."""
    res = _post(api_client, corpus, "quelle est la durée du contrat ?")
    assert res.status_code == 500


def test_clarification_creates_no_rag_query_log(api_client, corpus, no_ai):
    before = RagQueryLog.objects.count()

    _post(api_client, corpus, GLOBAL_Q)
    _stream(api_client, corpus, "analyse tout le corpus")

    assert RagQueryLog.objects.count() == before


# --------------------------------------------------------------------------- #
# Parité routeur ON / OFF
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", [False, True])
def test_router_on_off_parity(api_client, corpus, settings, flag):
    settings.ACTIONS_ROUTER_ENABLED = flag
    res = _post(api_client, corpus, GLOBAL_Q)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_mode_choice"
    assert res.data["log_id"] is None


# --------------------------------------------------------------------------- #
# Sécurité
# --------------------------------------------------------------------------- #

def test_no_access_learns_nothing(rag_space):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    stranger = U.objects.create_user(username="stranger2", password="pw", email="s2@e.com")
    client = APIClient()
    client.force_authenticate(user=stranger)

    res = _post(client, rag_space, GLOBAL_Q)
    assert res.status_code in (403, 404)
    body = str(res.data).lower()
    for leak in ("documents considérés", "extraits", "corpus_mode_choice"):
        assert leak not in body


def test_clarification_exposes_no_sensitive_content(api_client, corpus):
    d = _doc(corpus, name="secret-dossier.pdf", sha="zz")
    _chunk(corpus, d, text="TEXTE_CONFIDENTIEL_DU_CHUNK", idx=9)

    answer = _post(api_client, corpus, GLOBAL_Q).data["answer"].lower()

    assert "texte_confidentiel_du_chunk" not in answer
    assert "secret-dossier" not in answer  # aucun nom de fichier
    for banned in ("ollama", "openai", "gateway", "provider", "celery", "queue",
                   "/api/", "settings", "traceback", "top_k", "embedding"):
        assert banned not in answer, f"exposé : {banned}"
