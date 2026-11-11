"""L4 — activation L3, lancement du job, reprise après rechargement, API."""
import json
import re

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.chat.models import ChatMessage
from apps.rag_core.analysis import global_search as gs
from apps.rag_core.analysis.registry import GLOBAL_SEARCH
from apps.rag_core.analysis.runner import execute_job
from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.corpus_analysis_job import AnalysisStatus, CorpusAnalysisJob
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Membership

pytestmark = pytest.mark.django_db

# Demande globale AVEC sujet -> compatible L4.
GLOBAL_WITH_TOPIC = "recherche dans toute la base les informations sur la cybersécurité"
# Demandes globales SANS sujet -> restent indisponibles.
GLOBAL_NO_TOPIC = "résume tous les documents"


@pytest.fixture
def corpus(rag_space):
    for i, t in enumerate([
        "La cybersécurité du système est renforcée par un pare-feu.",
        "Le plan de cybersécurité prévoit un audit annuel.",
        "Le catalogue produit contient douze références.",
    ]):
        d = Document.objects.create(rag_space=rag_space, filename=f"d{i}.txt",
                                    status=DocumentStatus.READY, sha256=f"s{i}")
        Chunk.objects.create(rag_space=rag_space, document=d, chunk_index=i,
                             text=t, page_number=1)
    Document.objects.create(rag_space=rag_space, filename="ko.txt",
                            status=DocumentStatus.FAILED, sha256="")
    return rag_space


@pytest.fixture(autouse=True)
def no_enqueue(monkeypatch):
    """Aucun broker en test : on n'exécute pas le job à la création."""
    from apps.rag_core import tasks

    monkeypatch.setattr(tasks.run_corpus_analysis_job, "apply_async",
                        lambda *a, **kw: None)


@pytest.fixture
def fake_llm(monkeypatch):
    def fake(**kw):
        ids = re.findall(r"\[([0-9a-f\-]{36})\]", kw["prompt"])
        items = [{"statement": "La cybersécurité fait l’objet d’un audit.",
                  "source_chunk_ids": ids[:1], "confidence": 0.9}] if ids else []
        return {"text": json.dumps({"items": items}), "provider": "s", "model": "m",
                "usage": {"prompt_tokens": 50, "completion_tokens": 10}}

    monkeypatch.setattr(gs, "_generate", lambda **kw: fake(**kw))


def _post(client, space, q, thread_id=None):
    body = {"rag_space_id": str(space.id), "question": q}
    if thread_id:
        body["thread_id"] = str(thread_id)
    return client.post("/api/v1/chat/", body, format="json")


# --------------------------------------------------------------------------- #
# CHAT1..CHAT4 — activation conditionnelle
# --------------------------------------------------------------------------- #

def test_compatible_request_offers_the_global_analysis(api_client, corpus):
    """CHAT1 — une demande globale AVEC sujet propose l'analyse globale."""
    res = _post(api_client, corpus, GLOBAL_WITH_TOPIC)

    assert res.data["answer_kind"] == "corpus_mode_choice"
    answer = res.data["answer"]
    assert "Une analyse globale peut être lancée" in answer
    assert "couverture mesurée" in answer
    # Lot 3 — l'analyse ne se déclenche plus en RECOPIANT une commande : elle est
    # offerte comme une action structurée, que le frontend rend en bouton.
    assert "`analyse complète`" not in answer
    assert res.data["awaiting_corpus_choice"] is True
    assert res.data["global_analysis_available"] is True
    # L'avertissement lexical est toujours là.
    assert "peut ne pas reconnaître toutes les formulations" in answer
    # Et la promesse d'indisponibilité a disparu POUR CE CAS.
    assert "n’est pas encore disponible" not in answer


def test_request_without_topic_stays_unavailable(api_client, corpus):
    """CHAT3 / CHAT4 — une demande globale SANS sujet reste indisponible."""
    res = _post(api_client, corpus, GLOBAL_NO_TOPIC)

    assert res.data["answer_kind"] == "corpus_mode_choice"
    answer = res.data["answer"]
    assert "n’est pas encore disponible" in answer
    assert "précisez le sujet" in answer.lower()
    assert "Une analyse globale peut être lancée" not in answer


def test_confirmation_launches_a_global_search_job(api_client, corpus):
    """CHAT2 — « analyse complète » crée UN job corpus.global_search."""
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]

    res = _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_analysis_launched"
    assert "Analyse globale lancée" in res.data["answer"]
    assert res.data["analysis_id"]

    jobs = CorpusAnalysisJob.objects.filter(analysis_type=GLOBAL_SEARCH)
    assert jobs.count() == 1
    job = jobs.first()
    # La question d'ORIGINE est la requête, pas « analyse complète ».
    assert job.parameters["query"] == GLOBAL_WITH_TOPIC
    assert job.status == AnalysisStatus.PENDING
    # Lien DURABLE avec le fil.
    assert str(job.chat_thread_id) == str(thread_id)


def test_confirmation_without_topic_creates_no_job(api_client, corpus):
    """CHAT3 — aucune analyse n'est lancée pour une demande sans sujet."""
    thread_id = _post(api_client, corpus, GLOBAL_NO_TOPIC).data["thread_id"]

    res = _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    assert res.data["answer_kind"] == "corpus_full_unavailable"
    assert CorpusAnalysisJob.objects.count() == 0


def test_confirmation_out_of_context_creates_no_job(api_client, corpus):
    """« analyse complète » hors clarification : aucun job, aucune vieille question."""
    res = _post(api_client, corpus, "analyse complète")

    assert res.data.get("answer_kind") != "corpus_analysis_launched"
    assert CorpusAnalysisJob.objects.count() == 0


def test_replaying_the_confirmation_does_not_create_a_second_job(api_client, corpus):
    """CHAT7 — deux confirmations ne créent pas deux jobs."""
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    first = _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    # Nouvelle clarification + nouvelle confirmation, même corpus, même question.
    _post(api_client, corpus, GLOBAL_WITH_TOPIC, thread_id=thread_id)
    second = _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    assert CorpusAnalysisJob.objects.filter(analysis_type=GLOBAL_SEARCH).count() == 1
    assert second.data["analysis_id"] == first.data["analysis_id"]


@pytest.mark.parametrize("flag", [False, True])
def test_router_on_off_parity(api_client, corpus, settings, flag):
    """CHAT8 — comportement identique routeur ON et OFF."""
    settings.ACTIONS_ROUTER_ENABLED = flag
    res = _post(api_client, corpus, GLOBAL_WITH_TOPIC)
    assert res.data["answer_kind"] == "corpus_mode_choice"
    assert "Une analyse globale peut être lancée" in res.data["answer"]


def test_targeted_paths_are_untouched(api_client, corpus, monkeypatch):
    """CHAT5 / CHAT6 — les chemins ciblés existants ne bougent pas."""
    from apps.actions.router import ActionRouter

    r = ActionRouter()
    assert r.route("analyse ce document").action == "search.answer"
    assert r.route("liste tous les documents sur Nordexia").action == "search.list_documents"
    assert r.route("résume Rapport.pdf").action == "document.summarize"
    assert r.route("/aide").action == "help.capabilities"


def test_quick_mode_remains_explicitly_partial(api_client, corpus, monkeypatch):
    """CHAT10 — le mode rapide L3 reste inchangé et toujours partiel."""
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
        "status": "ok", "sources": [{"chunk_id": "c1", "document_id": "d1"}],
        "prompt": "P", "log_id": None, "answer_kind": "ok"})
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "Réponse.", "provider": "s", "model": "m", "usage": {}})
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    res = _post(api_client, corpus, "réponse rapide", thread_id=thread_id)

    assert res.data["answer"].startswith("> **Couverture partielle**")
    assert res.data["coverage"]["coverage_complete"] is False
    assert CorpusAnalysisJob.objects.count() == 0  # le mode rapide ne lance rien


# --------------------------------------------------------------------------- #
# Reprise après rechargement
# --------------------------------------------------------------------------- #

def test_thread_analyses_endpoint_enables_resume(api_client, corpus, fake_llm):
    """Le frontend retrouve le job APRÈS rechargement, via un lien DURABLE."""
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    launched = _post(api_client, corpus, "analyse complète", thread_id=thread_id)
    analysis_id = launched.data["analysis_id"]

    execute_job(analysis_id)

    # Nouveau client : rien en mémoire, rien en React.
    fresh = APIClient()
    fresh.force_authenticate(user=api_client.handler._force_user)
    res = fresh.get(f"/api/v1/chat/threads/{thread_id}/analyses/")

    assert res.status_code == 200
    analyses = res.data["analyses"]
    assert len(analyses) == 1
    assert analyses[0]["analysis_id"] == analysis_id
    assert analyses[0]["status"] == "succeeded"
    assert analyses[0]["result"]["items"]


def test_reloaded_message_still_carries_its_analysis_id(api_client, corpus, fake_llm):
    """La carte d'analyse doit pouvoir se raccrocher à SON message après un F5.

    Le lien fil -> job ne suffit pas : le frontend rend la carte sous le message
    qui porte l'``analysis_id``. Si l'historique ne le renvoie pas, la carte
    disparaît au rechargement alors que le job, lui, existe toujours.
    """
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    analysis_id = _post(api_client, corpus, "analyse complète", thread_id=thread_id).data[
        "analysis_id"
    ]

    history = api_client.get(f"/api/v1/chat/threads/{thread_id}/messages/")
    assert history.status_code == 200

    carriers = [m for m in history.data if m.get("analysis_id")]
    assert [m["analysis_id"] for m in carriers] == [analysis_id]
    assert carriers[0]["role"] == "assistant"

    # Les messages ordinaires ne portent aucun identifiant d'analyse.
    assert all(m["analysis_id"] is None for m in history.data if m not in carriers)


def test_reused_job_is_resumable_from_the_thread_that_cites_it(api_client, corpus, fake_llm):
    """Un job réutilisé par idempotence reste retrouvable depuis le NOUVEAU fil.

    L'idempotence rattache le job au fil où il a été créé. Rejouer la même question
    globale dans un autre fil y renvoie le même ``analysis_id`` : ce second fil doit
    donc, lui aussi, le retrouver au rechargement.
    """
    first = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    analysis_id = _post(api_client, corpus, "analyse complète", thread_id=first).data[
        "analysis_id"
    ]

    # Second fil (thread_id absent = nouveau fil), même question.
    second = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    replay = _post(api_client, corpus, "analyse complète", thread_id=second)

    assert second != first
    assert replay.data["analysis_id"] == analysis_id  # réutilisé, pas recréé
    assert CorpusAnalysisJob.objects.filter(analysis_type=GLOBAL_SEARCH).count() == 1

    res = api_client.get(f"/api/v1/chat/threads/{second}/analyses/")
    assert res.status_code == 200
    assert [a["analysis_id"] for a in res.data["analyses"]] == [analysis_id]


def test_thread_analyses_requires_access(corpus, rag_space, api_client, user, workspace):
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    Membership.objects.filter(workspace=workspace, user=user).delete()

    res = api_client.get(f"/api/v1/chat/threads/{thread_id}/analyses/")
    assert res.status_code == 404
    assert "cybersécurité" not in str(res.data).lower()


def test_another_user_cannot_read_a_thread_analysis(corpus, api_client):
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    _post(api_client, corpus, "analyse complète", thread_id=thread_id)

    U = get_user_model()
    stranger = U.objects.create_user(username="str", password="p", email="s@e.com")
    client = APIClient()
    client.force_authenticate(user=stranger)

    assert client.get(f"/api/v1/chat/threads/{thread_id}/analyses/").status_code == 404


# --------------------------------------------------------------------------- #
# API — création directe
# --------------------------------------------------------------------------- #

def _create(client, space, **body):
    return client.post(f"/api/v1/rag-spaces/{space.id}/analyses/",
                       {"analysis_type": GLOBAL_SEARCH, **body}, format="json")


def test_api_create_global_search(api_client, corpus):
    res = _create(api_client, corpus,
                  parameters={"query": GLOBAL_WITH_TOPIC}, request_id="r1")
    assert res.status_code == 202
    assert res.data["analysis_type"] == GLOBAL_SEARCH


def test_api_rejects_a_query_without_topic(api_client, corpus):
    res = _create(api_client, corpus, parameters={"query": "tous les documents"})
    assert res.status_code == 400
    assert "global_search_topic_required" in str(res.data)
    assert CorpusAnalysisJob.objects.count() == 0


@pytest.mark.parametrize("forbidden", [
    "top_k", "candidate_limit", "batch_size", "provider", "model",
    "temperature", "fallback", "fts_configuration",
])
def test_client_cannot_set_server_owned_parameters(api_client, corpus, forbidden):
    """Ces paramètres restent des décisions SERVEUR."""
    res = _create(api_client, corpus,
                  parameters={"query": GLOBAL_WITH_TOPIC, forbidden: 999})
    assert res.status_code == 400
    assert CorpusAnalysisJob.objects.count() == 0


def test_api_concurrency_limit_returns_409(api_client, corpus, settings):
    settings.CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE = 1

    assert _create(api_client, corpus,
                   parameters={"query": GLOBAL_WITH_TOPIC}, request_id="r1"
                   ).status_code == 202

    res = _create(api_client, corpus,
                  parameters={"query": "les délais de livraison"}, request_id="r2")

    assert res.status_code == 409
    assert res.data["detail"] == "analytics_concurrency_limit"
    assert CorpusAnalysisJob.objects.filter(status="pending").count() == 1


def test_result_payload_carries_search_metrics(api_client, corpus, fake_llm):
    created = _create(api_client, corpus,
                      parameters={"query": GLOBAL_WITH_TOPIC}, request_id="r1")
    aid = created.data["analysis_id"]
    execute_job(aid)

    d = api_client.get(f"/api/v1/analyses/{aid}/").data

    assert d["status"] == "succeeded"
    s = d["result"]["search"]
    assert s["chunks_scanned"] == 3          # tout le périmètre exploitable
    assert s["candidate_chunks"] == 2        # retenus par le préfiltre
    assert s["processed_chunks"] == 2
    assert d["coverage"]["coverage_complete"] is False  # 1 document en échec
    assert d["coverage"]["items_without_source"] == 0


# --------------------------------------------------------------------------- #
# Lot 3 — le choix vient d'un BOUTON : action structurée, plus aucune saisie
# --------------------------------------------------------------------------- #
QUICK = "quick_response"
FULL = "global_analysis"


def _choose(client, space, choice, thread_id=None):
    """Ce qu'envoie un bouton : un identifiant d'action, et RIEN d'autre.

    Ni la question d'origine, ni un texte à interpréter. Le serveur résout tout.
    """
    body = {"rag_space_id": str(space.id), "question": "", "corpus_choice": choice}
    if thread_id:
        body["thread_id"] = str(thread_id)
    return client.post("/api/v1/chat/", body, format="json")


def test_mode_choice_advertises_the_buttons(api_client, corpus):
    """La clarification dit au frontend qu'elle attend un choix, et lequel est possible."""
    res = _post(api_client, corpus, GLOBAL_WITH_TOPIC)

    assert res.data["answer_kind"] == "corpus_mode_choice"
    assert res.data["awaiting_corpus_choice"] is True
    assert res.data["global_analysis_available"] is True
    # Le message n'ordonne plus de recopier une commande.
    assert "Répondez" not in res.data["answer"]


def test_mode_choice_hides_the_full_button_without_a_topic(api_client, corpus):
    res = _post(api_client, corpus, GLOBAL_NO_TOPIC)

    assert res.data["awaiting_corpus_choice"] is True
    assert res.data["global_analysis_available"] is False


def test_button_launches_the_global_analysis(api_client, corpus):
    """Le clic produit EXACTEMENT ce que produisait la saisie « analyse complète »."""
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]

    res = _choose(api_client, corpus, FULL, thread_id=thread_id)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_analysis_launched"
    assert res.data["analysis_id"]

    jobs = CorpusAnalysisJob.objects.filter(analysis_type=GLOBAL_SEARCH)
    assert jobs.count() == 1
    # La question d'ORIGINE est la requête — elle n'a jamais transité par le client.
    assert jobs.first().parameters["query"] == GLOBAL_WITH_TOPIC
    assert str(jobs.first().chat_thread_id) == str(thread_id)


def test_button_persists_a_readable_choice_never_a_technical_id(api_client, corpus):
    """L'historique doit rester lisible : jamais `global_analysis` dans le fil."""
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    _choose(api_client, corpus, FULL, thread_id=thread_id)

    contents = list(
        ChatMessage.objects.filter(thread_id=thread_id, role="user")
        .order_by("created_at")
        .values_list("content", flat=True)
    )
    assert contents == [GLOBAL_WITH_TOPIC, "Analyse complète"]
    assert FULL not in " ".join(contents)


def test_button_quick_replays_the_original_question(api_client, corpus, monkeypatch):
    """« Réponse rapide » rejoue la question d'origine, et ne crée aucun job."""
    seen = {}

    def fake_chat_query(*, user, rag_space, **kw):
        seen.update(kw)
        return {"answer": "ok", "sources": [], "thread_id": str(kw.get("thread_id")),
                "status": "ok", "log_id": None}

    import api.v1.chat.views as views
    monkeypatch.setattr(views, "chat_query", fake_chat_query)

    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    res = _choose(api_client, corpus, QUICK, thread_id=thread_id)

    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_quick_answer"
    # Le RAG reçoit la question d'ORIGINE, jamais le libellé du bouton.
    assert seen["question"] == GLOBAL_WITH_TOPIC
    assert seen["persisted_question"] == "Réponse rapide"
    assert CorpusAnalysisJob.objects.count() == 0


def test_double_click_creates_a_single_job_and_then_409s(api_client, corpus):
    """Le double clic est la raison d'être de l'idempotence.

    Le second appel ne trouve plus de clarification en attente : rien n'est exécuté,
    et surtout le libellé du bouton ne part pas dans le RAG.
    """
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]

    first = _choose(api_client, corpus, FULL, thread_id=thread_id)
    second = _choose(api_client, corpus, FULL, thread_id=thread_id)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.data["detail"] == "corpus_choice_expired"
    assert CorpusAnalysisJob.objects.filter(analysis_type=GLOBAL_SEARCH).count() == 1


def test_a_choice_without_a_pending_clarification_executes_nothing(api_client, corpus):
    """Hors contexte : 409, aucun job, aucun appel au RAG."""
    res = _choose(api_client, corpus, FULL)

    assert res.status_code == 409
    assert CorpusAnalysisJob.objects.count() == 0


def test_a_choice_cannot_reach_another_users_thread(api_client, corpus):
    """Le fil d'autrui est invisible : le choix n'y trouve aucune clarification."""
    from django.contrib.auth import get_user_model

    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]

    intruder = get_user_model().objects.create_user(
        username="intrus", email="intrus@x.io", password="x"
    )
    Membership.objects.create(user=intruder, workspace=corpus.workspace, role="member")
    other = APIClient()
    other.force_authenticate(intruder)

    res = _choose(other, corpus, FULL, thread_id=thread_id)

    assert res.status_code == 409
    assert CorpusAnalysisJob.objects.count() == 0


def test_the_buttons_come_back_after_a_reload_and_leave_once_consumed(api_client, corpus):
    """Reprise après F5 — sans migration, sans analyse du Markdown.

    C'est le serveur qui recalcule si un choix est encore en attente.
    """
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]

    reloaded = api_client.get(f"/api/v1/chat/threads/{thread_id}/messages/").data
    assert reloaded[-1]["role"] == "assistant"
    assert reloaded[-1]["awaiting_corpus_choice"] is True
    assert reloaded[-1]["global_analysis_available"] is True
    # Les messages plus anciens ne portent jamais de boutons.
    assert all(m["awaiting_corpus_choice"] is False for m in reloaded[:-1])

    _choose(api_client, corpus, FULL, thread_id=thread_id)

    after = api_client.get(f"/api/v1/chat/threads/{thread_id}/messages/").data
    assert all(m["awaiting_corpus_choice"] is False for m in after), (
        "un choix consommé ne doit jamais réafficher ses boutons"
    )
    # La carte d'analyse, elle, revient bien.
    assert after[-1]["analysis_id"]


def test_button_and_typed_command_are_strictly_equivalent(api_client, corpus):
    """Le filet de compatibilité textuelle produit le MÊME résultat que le bouton."""
    t1 = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    typed = _post(api_client, corpus, "analyse complète", thread_id=t1)

    t2 = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]
    clicked = _choose(api_client, corpus, FULL, thread_id=t2)

    assert typed.data["answer_kind"] == clicked.data["answer_kind"]
    assert typed.data["answer"] == clicked.data["answer"]
    # Idempotence du moteur : même question, même corpus -> un seul job réutilisé.
    assert typed.data["analysis_id"] == clicked.data["analysis_id"]


def test_button_choice_is_identical_in_sse(api_client, corpus):
    """Parité sync/SSE : le choix structuré emprunte exactement le même chemin."""
    thread_id = _post(api_client, corpus, GLOBAL_WITH_TOPIC).data["thread_id"]

    res = api_client.post(
        "/api/v1/chat/stream/",
        {"rag_space_id": str(corpus.id), "question": "", "corpus_choice": FULL,
         "thread_id": str(thread_id)},
        format="json",
    )
    body = b"".join(res.streaming_content).decode("utf-8")

    assert "corpus_analysis_launched" in body
    assert CorpusAnalysisJob.objects.filter(analysis_type=GLOBAL_SEARCH).count() == 1


def test_expired_choice_in_sse_yields_an_error_and_no_job(api_client, corpus):
    res = api_client.post(
        "/api/v1/chat/stream/",
        {"rag_space_id": str(corpus.id), "question": "", "corpus_choice": FULL},
        format="json",
    )
    body = b"".join(res.streaming_content).decode("utf-8")

    assert "corpus_choice_expired" in body
    assert CorpusAnalysisJob.objects.count() == 0


def test_an_unknown_choice_is_rejected_by_the_contract(api_client, corpus):
    """Identifiants d'action fermés : rien d'autre n'entre."""
    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(corpus.id), "question": "", "corpus_choice": "rm_-rf"},
        format="json",
    )
    assert res.status_code == 400
