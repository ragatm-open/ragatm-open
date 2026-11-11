"""Lot 1 — aide déterministe : preuves d'absence d'IA + contrat HTTP/SSE.

Couvre T4 à T13 :
  - T4/T5  parité ``ACTIONS_ROUTER_ENABLED`` OFF/ON (pas de double réponse)
  - T6     aucun retrieval / embedding / LLM (chemins IA monkeypatchés en ``raise``)
  - T7     aucun ``RagQueryLog`` créé
  - T8     aucune tâche Celery de reformulation enfilée
  - T9     persistance (1 user + 1 assistant, sources=[], rag_query_log_id=None)
  - T10    idempotence sur ``request_id``
  - T11/12 contrat SSE et sync
  - T13    aide disponible même si le service IA est indisponible
"""
from __future__ import annotations

import json

import pytest

from apps.audit.models import RagQueryLog
from apps.chat.models import ChatMessage
from apps.help.catalog import render_help_markdown

pytestmark = pytest.mark.django_db

CHAT_URL = "/api/v1/chat/"
STREAM_URL = "/api/v1/chat/stream/"


class ForbiddenAIPath(AssertionError):
    """Levée si un chemin IA/retrieval est touché par une requête d'aide."""


def _forbid(*_args, **_kwargs):
    raise ForbiddenAIPath("FORBIDDEN_AI_PATH")


@pytest.fixture
def no_ai(monkeypatch):
    """Neutralise TOUS les chemins interdits, au point d'usage réel.

    Les symboles sont patchés là où ils sont *importés* (le module consommateur),
    pas seulement là où ils sont définis : c'est la seule façon de prouver l'absence
    d'appel, et non l'absence de trafic réseau.
    """
    import apps.chat.services.chat_service as cs
    import apps.rag_core.services.query.orchestrator as orch
    import apps.chat.services.rephrasing_async as ra

    # Retrieval
    monkeypatch.setattr(cs, "query_rag", _forbid, raising=False)
    monkeypatch.setattr(cs, "_run_retrieval", _forbid, raising=False)
    monkeypatch.setattr(orch, "query_rag", _forbid, raising=False)
    # Embeddings (au point d'usage dans l'orchestrateur ET à la source)
    monkeypatch.setattr(orch, "embed_texts", _forbid, raising=False)
    import apps.llm_router.embeddings as emb
    monkeypatch.setattr(emb, "embed_texts", _forbid, raising=False)
    # Génération LLM
    monkeypatch.setattr(cs, "generate_text", _forbid, raising=False)
    monkeypatch.setattr(cs, "generate_text_stream", _forbid, raising=False)
    import apps.llm_router.router as llm
    monkeypatch.setattr(llm, "generate_text", _forbid, raising=False)
    monkeypatch.setattr(llm, "generate_text_stream", _forbid, raising=False)
    # Routeur news + synthèse news (appels LLM)
    monkeypatch.setattr(cs, "llm_route_news_or_none", _forbid, raising=False)
    import apps.chat.services.intents.news as news
    monkeypatch.setattr(news, "llm_route_news_or_none", _forbid, raising=False)
    monkeypatch.setattr(news, "summarize_news_with_llm", _forbid, raising=False)
    # Tâche Celery de reformulation
    monkeypatch.setattr(cs, "enqueue_no_answer_suggestions", _forbid, raising=False)
    monkeypatch.setattr(ra, "enqueue_no_answer_suggestions", _forbid, raising=False)


@pytest.fixture
def flag_on(settings):
    settings.ACTIONS_ROUTER_ENABLED = True
    return settings


@pytest.fixture
def flag_off(settings):
    settings.ACTIONS_ROUTER_ENABLED = False
    return settings


def _post(api_client, rag_space, question, **extra):
    return api_client.post(
        CHAT_URL,
        {"rag_space_id": str(rag_space.id), "question": question, **extra},
        format="json",
    )


def _stream(api_client, rag_space, question, **headers):
    res = api_client.post(
        STREAM_URL,
        {"rag_space_id": str(rag_space.id), "question": "/aide" if question is None else question},
        format="json",
        **headers,
    )
    body = b"".join(res.streaming_content).decode("utf-8")
    return res, body


def _sse_events(body: str) -> list[tuple[str, dict]]:
    out = []
    for frame in body.strip().split("\n\n"):
        if not frame.strip():
            continue
        name, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        out.append((name, data))
    return out


# ---------------------------------------------------------------------------
# T4 / T12 — sync, flag OFF (le défaut de production)
# ---------------------------------------------------------------------------
def test_help_sync_router_off(api_client, rag_space, flag_off, no_ai):
    res = _post(api_client, rag_space, "/aide")

    assert res.status_code == 200
    body = res.json()
    assert body["answer_kind"] == "help"
    assert body["log_id"] is None
    assert body["sources"] == []
    assert body["answer"].startswith("# ")
    assert "Résumer" in body["answer"]


# ---------------------------------------------------------------------------
# T5 — sync, flag ON : même résultat, pas de double réponse ni double persistance
# ---------------------------------------------------------------------------
def test_help_sync_router_on_is_identical(api_client, rag_space, flag_on, no_ai):
    res = _post(api_client, rag_space, "/aide")

    assert res.status_code == 200
    body = res.json()
    assert body["answer_kind"] == "help"
    assert body["log_id"] is None
    assert ChatMessage.objects.filter(role="assistant").count() == 1
    assert ChatMessage.objects.filter(role="user").count() == 1


def test_help_parity_off_on(api_client, rag_space, settings, no_ai):
    settings.ACTIONS_ROUTER_ENABLED = False
    off = _post(api_client, rag_space, "/aide").json()["answer"]
    settings.ACTIONS_ROUTER_ENABLED = True
    on = _post(api_client, rag_space, "/aide").json()["answer"]
    assert off == on


# ---------------------------------------------------------------------------
# T6 / T13 — aucun retrieval, embedding, LLM (chemins en raise)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question", ["/aide", "?", "que puis-je faire ?", "what can I ask?"])
def test_help_never_touches_ai_paths(api_client, rag_space, flag_off, no_ai, question):
    res = _post(api_client, rag_space, question)
    assert res.status_code == 200
    assert res.json()["answer_kind"] == "help"


def test_help_never_touches_ai_paths_streaming(api_client, rag_space, flag_off, no_ai):
    res, body = _stream(api_client, rag_space, "/aide")
    assert res.status_code == 200
    assert "FORBIDDEN_AI_PATH" not in body
    events = dict(_sse_events(body))
    assert events["done"]["answer_kind"] == "help"


def test_control_a_normal_question_would_hit_the_ai_path(api_client, rag_space, flag_off, no_ai):
    """Contrôle négatif : sans lui, le test T6 pourrait passer pour une mauvaise raison.

    Une question documentaire DOIT, elle, atteindre un chemin IA — ce qui prouve que
    les monkeypatches sont bien posés et que l'aide les évite réellement.
    """
    res = _post(api_client, rag_space, "quelle est la durée du contrat ?")
    assert res.status_code == 500  # ForbiddenAIPath a bien été levée puis convertie


# ---------------------------------------------------------------------------
# T7 — aucun RagQueryLog
# ---------------------------------------------------------------------------
def test_help_creates_no_rag_query_log(api_client, rag_space, flag_off, no_ai):
    before = RagQueryLog.objects.count()
    _post(api_client, rag_space, "/aide")
    _stream(api_client, rag_space, "/help")
    assert RagQueryLog.objects.count() == before


# ---------------------------------------------------------------------------
# T8 — aucune tâche Celery de reformulation
# ---------------------------------------------------------------------------
def test_help_enqueues_no_rephrasing_task(api_client, rag_space, flag_off, monkeypatch):
    calls = []
    import apps.chat.services.chat_service as cs
    monkeypatch.setattr(
        cs, "enqueue_no_answer_suggestions", lambda **k: calls.append(k), raising=False
    )
    import apps.chat.tasks as tasks
    if hasattr(tasks, "generate_rephrasing_suggestions_task"):
        monkeypatch.setattr(
            tasks.generate_rephrasing_suggestions_task,
            "delay",
            lambda *a, **k: calls.append(("delay", a, k)),
            raising=False,
        )

    _post(api_client, rag_space, "/aide")
    _stream(api_client, rag_space, "/aide")

    assert calls == []


# ---------------------------------------------------------------------------
# T9 — persistance
# ---------------------------------------------------------------------------
def test_help_persists_user_and_assistant_messages(api_client, rag_space, flag_off, no_ai):
    _stream(api_client, rag_space, "/aide")

    user_msgs = ChatMessage.objects.filter(role="user")
    assistant_msgs = ChatMessage.objects.filter(role="assistant")
    assert user_msgs.count() == 1
    assert assistant_msgs.count() == 1

    a = assistant_msgs.first()
    assert a.sources == []
    assert a.rag_query_log_id is None
    assert a.content.startswith("# ")


# ---------------------------------------------------------------------------
# T10 — idempotence sur request_id
# ---------------------------------------------------------------------------
def test_help_is_idempotent_on_request_id(api_client, rag_space, flag_off, no_ai):
    """Un retry (même thread + même request_id) ne duplique pas les messages.

    L'idempotence est scopée au fil, exactement comme le chemin listing historique
    (``_ensure_thread_and_persist_messages`` filtre sur ``(thread, request_id, role)``).
    Un appel sans ``thread_id`` crée par définition un nouveau fil.
    """
    headers = {"HTTP_X_REQUEST_ID": "req-help-1"}

    _res, body = _stream(api_client, rag_space, "/aide", **headers)
    thread_id = dict(_sse_events(body))["done"]["thread_id"]

    # Retry du même request_id, dans le même fil.
    res = api_client.post(
        STREAM_URL,
        {"rag_space_id": str(rag_space.id), "question": "/aide", "thread_id": thread_id},
        format="json",
        **headers,
    )
    b"".join(res.streaming_content)

    assert ChatMessage.objects.filter(request_id="req-help-1", role="user").count() == 1
    assert ChatMessage.objects.filter(request_id="req-help-1", role="assistant").count() == 1
    assert ChatMessage.objects.filter(thread_id=thread_id).count() == 2


# ---------------------------------------------------------------------------
# T11 — contrat SSE inchangé
# ---------------------------------------------------------------------------
def test_help_streaming_contract(api_client, rag_space, flag_off, no_ai):
    _res, body = _stream(api_client, rag_space, "/aide")
    events = _sse_events(body)
    names = [n for n, _ in events]

    assert names == ["meta", "sources", "token", "done"]

    data = dict(events)
    assert data["sources"]["sources"] == []
    assert data["token"]["delta"].startswith("# ")
    assert data["done"]["status"] == "ok"
    assert data["done"]["answer_kind"] == "help"
    assert data["done"]["log_id"] is None
    assert "error" not in names


# ---------------------------------------------------------------------------
# HELP1 — toutes les commandes d'aide répondent, en sync
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["/help", "/aide", "/?", "?", "help", "aide"])
def test_every_help_command_answers(api_client, rag_space, flag_off, no_ai, command):
    res = _post(api_client, rag_space, command)
    assert res.status_code == 200
    body = res.json()
    assert body["answer_kind"] == "help"
    # Le détail de l'analyse a migré vers `/help analyse` (Lot 2) : le niveau 1
    # annonce les capacités et le menu, pas les états ni la couverture.
    assert "**Analyser tout le corpus**" in body["answer"]
    assert "`/help analyse`" in body["answer"]


# ---------------------------------------------------------------------------
# HELP7 / Phase 4 — SOURCE DE VÉRITÉ UNIQUE : sync ≡ SSE ≡ message persisté
# ---------------------------------------------------------------------------
def test_sync_sse_and_persistence_share_the_exact_same_content(
    api_client, rag_space, flag_off, no_ai
):
    """Trois chemins, un seul texte. C'est ce qui empêche l'aide de diverger.

    Le contenu est comparé au rendu du catalogue lui-même : aucune copie du Markdown
    ne peut survivre ailleurs sans faire échouer ce test.
    """
    from apps.help.catalog import render_help_markdown

    expected = render_help_markdown()

    sync = _post(api_client, rag_space, "/aide").json()["answer"]
    _res, body = _stream(api_client, rag_space, "/aide")
    sse = dict(_sse_events(body))["token"]["delta"]
    persisted = ChatMessage.objects.filter(role="assistant").first().content

    assert sync == expected
    assert sse == expected
    assert persisted == expected


def test_help_reloaded_from_the_thread_is_unchanged(api_client, rag_space, flag_off, no_ai):
    """HELP10 — après rechargement du fil, l'aide affichée est identique."""
    from apps.help.catalog import render_help_markdown

    thread_id = _post(api_client, rag_space, "/aide").json()["thread_id"]

    history = api_client.get(f"/api/v1/chat/threads/{thread_id}/messages/")
    assert history.status_code == 200
    assistant = [m for m in history.data if m["role"] == "assistant"]

    assert len(assistant) == 1
    assert assistant[0]["content"] == render_help_markdown()
    assert assistant[0]["sources"] == []
    # HELP : aucune carte d'analyse ne doit apparaître sous une réponse d'aide.
    assert assistant[0]["analysis_id"] is None


def test_help_creates_no_analysis_job(api_client, rag_space, flag_off, no_ai):
    """L'aide décrit les analyses globales — elle n'en déclenche aucune."""
    from apps.rag_core.models.corpus_analysis_job import CorpusAnalysisJob

    _post(api_client, rag_space, "/aide")
    _stream(api_client, rag_space, "/help")

    assert CorpusAnalysisJob.objects.count() == 0


# ---------------------------------------------------------------------------
# Non-régression : les négatifs ne passent PAS par l'aide
# ---------------------------------------------------------------------------
def test_listing_still_works_and_is_not_help(api_client, rag_space, flag_off):
    res = _post(api_client, rag_space, "liste les documents sur Nordexia")
    assert res.status_code == 200
    assert res.json().get("answer_kind") != "help"


# ---------------------------------------------------------------------------
# Lot 2 — sous-commandes thématiques : sync, SSE, parité, sujet inconnu
# ---------------------------------------------------------------------------
_TOPIC_MARKERS = {
    "analyse": "## Choisir entre réponse rapide et analyse globale",
    "documents": "## Lister les documents",
    "sources": "## Remonter aux sources",
    "limites": "## Analyses globales non encore disponibles",
}


@pytest.mark.parametrize("topic", sorted(_TOPIC_MARKERS))
def test_each_subcommand_answers_in_sync(api_client, rag_space, flag_off, no_ai, topic):
    """Aucune IA, aucun retrieval : le `no_ai` lève si l'un d'eux est touché."""
    res = _post(api_client, rag_space, f"/help {topic}")

    assert res.status_code == 200
    body = res.json()
    assert body["answer_kind"] == "help"
    assert body["log_id"] is None
    assert body["sources"] == []
    assert _TOPIC_MARKERS[topic] in body["answer"]


@pytest.mark.parametrize("topic", sorted(_TOPIC_MARKERS))
def test_each_subcommand_answers_in_sse(api_client, rag_space, flag_off, no_ai, topic):
    _res, body = _stream(api_client, rag_space, f"/help {topic}")
    events = _sse_events(body)

    assert [n for n, _ in events] == ["meta", "sources", "token", "done"]
    data = dict(events)
    assert data["done"]["answer_kind"] == "help"
    assert _TOPIC_MARKERS[topic] in data["token"]["delta"]


@pytest.mark.parametrize("topic", sorted(_TOPIC_MARKERS))
def test_subcommand_sync_and_sse_are_identical(api_client, rag_space, flag_off, no_ai, topic):
    """Le sujet doit se propager IDENTIQUEMENT dans les deux chemins."""
    sync = _post(api_client, rag_space, f"/help {topic}").json()["answer"]
    _res, body = _stream(api_client, rag_space, f"/help {topic}")
    streamed = dict(_sse_events(body))["token"]["delta"]

    assert sync == streamed == render_help_markdown(topic)


@pytest.mark.parametrize("flag", ["flag_off", "flag_on"])
@pytest.mark.parametrize("topic", sorted(_TOPIC_MARKERS))
def test_subcommand_parity_router_off_on(api_client, rag_space, no_ai, request, flag, topic):
    request.getfixturevalue(flag)
    body = _post(api_client, rag_space, f"/help {topic}").json()
    assert body["answer"] == render_help_markdown(topic)


@pytest.mark.parametrize("alias,canonical", [("analysis", "analyse"), ("docs", "documents"),
                                             ("limits", "limites"), ("source", "sources")])
def test_aliases_answer_like_their_topic(api_client, rag_space, flag_off, no_ai, alias, canonical):
    body = _post(api_client, rag_space, f"/help {alias}").json()
    assert body["answer"] == render_help_markdown(canonical)


def test_unknown_topic_returns_the_overview_and_never_the_rag(
    api_client, rag_space, flag_off, no_ai
):
    """`/help bidule` : ni erreur, ni RAG, ni LLM — juste l'aide principale.

    `no_ai` fait LEVER tout appel IA ou retrieval : un 200 prouve qu'aucun n'a eu lieu.
    """
    res = _post(api_client, rag_space, "/help bidule")

    assert res.status_code == 200
    body = res.json()
    assert body["answer_kind"] == "help"
    assert body["answer"] == render_help_markdown()
    assert body["log_id"] is None


def test_subcommands_create_no_log_no_task_no_analysis(
    api_client, rag_space, flag_off, no_ai
):
    from apps.rag_core.models.corpus_analysis_job import CorpusAnalysisJob

    before = RagQueryLog.objects.count()
    for topic in sorted(_TOPIC_MARKERS):
        assert _post(api_client, rag_space, f"/help {topic}").status_code == 200

    assert RagQueryLog.objects.count() == before
    assert CorpusAnalysisJob.objects.count() == 0


def test_slash_help_with_a_document_like_topic_still_never_reaches_the_rag(
    api_client, rag_space, flag_off, no_ai
):
    """Le cas qui partait dans le RAG avant le Lot 2.

    « /help résumer ce document » ressemble à une demande documentaire, mais commence
    par `/help` : c'est une demande d'aide. `no_ai` lèverait si le RAG était atteint.
    """
    res = _post(api_client, rag_space, "/help résumer ce document")

    assert res.status_code == 200
    assert res.json()["answer_kind"] == "help"


# ---------------------------------------------------------------------------
# Issus de l'arbre racine (lot C) — couverture absente du corpus ci-dessus.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question", ["/aide", "/help", "?", "que puis-je faire ?"])
def test_help_sync_is_deterministic_and_ai_free(api_client, rag_space, flag_off, no_ai, question):
    """Contrat STRICT étendu à plusieurs formulations.

    `test_help_sync_router_off` ne le vérifie que pour `/aide` ; les tests
    multi-formulations ne contrôlent, eux, que le statut et `answer_kind`.
    """
    res = api_client.post(
        CHAT_URL,
        {"rag_space_id": str(rag_space.id), "question": question},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["answer_kind"] == "help"
    assert res.data["sources"] == []
    assert res.data["log_id"] is None
    assert res.data["answer"].startswith("# ")


@pytest.mark.parametrize(
    "question",
    [
        "aide-moi à résumer ce document",
        "donne-moi de l'aide sur ce contrat",
        "quelles fonctionnalités sont décrites dans ce PDF ?",
    ],
)
def test_help_does_not_hijack_document_questions(api_client, rag_space, question):
    """Anti-faux-positif au niveau HTTP : ces requêtes ne doivent pas répondre 'help'.

    Volontairement SANS `no_ai` : l'objet du test est que ces formulations suivent le
    chemin documentaire normal. Sous `no_ai`, ce chemin lèverait — le test ne prouverait
    plus rien. Il subsume l'ancien `test_natural_document_request_is_still_not_help`,
    qui ne couvrait que la première formulation.
    """
    res = api_client.post(
        CHAT_URL, {"rag_space_id": str(rag_space.id), "question": question}, format="json"
    )
    assert res.status_code == 200
    assert res.data.get("answer_kind") != "help"
