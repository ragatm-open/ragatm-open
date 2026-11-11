"""Lot E4 — la garde analytique Excel vue depuis HTTP.

Ce que ces tests prouvent, et que le classificateur ne peut pas prouver seul :

1. la garde intercepte **AVANT** tout retrieval, tout embedding et tout appel LLM —
   c'est ce qui la distingue d'une consigne dans un prompt ;
2. **aucun `RagQueryLog` documentaire** n'est créé : aucune recherche n'a eu lieu ;
3. sync et SSE renvoient **le même contenu** ;
4. le comportement est **identique routeur ON et OFF** ;
5. la réponse **survit au F5**, sans reclassifier ni rappeler le LLM ;
6. les questions locales sur un classeur passent toujours par le RAG normal.
"""
import json

import pytest

from apps.audit.models import RagQueryLog
from apps.chat.models import ChatMessage
from apps.chat.services.excel_guard import ANSWER_KIND

pytestmark = pytest.mark.django_db

BLOCKED_Q = "Quel est le budget total par direction dans ce classeur ?"
LOCAL_Q = "Quel est le budget du projet Atlas dans ce classeur ?"

EXCEL_CHUNK = {
    "chunk_id": "c1",
    "document_id": "d1",
    "text": "| Atlas | 250000 |",
    "score": 0.9,
    "source": {
        "document_id": "d1",
        "filename": "Budget_2026.xlsx",
        "page_number": None,
        "meta": {"kind": "excel_table", "sheet_name": "Synthèse", "cell_range": "B8:F17"},
    },
}


@pytest.fixture
def no_llm_no_retrieval(monkeypatch):
    """Sentinelles : tout appel au retrieval ou au LLM fait ÉCHOUER le test.

    C'est l'invariant du lot. Une garde qui laisserait passer un embedding ou un
    appel LLM ne serait qu'un filtre cosmétique.
    """
    import apps.chat.services.chat_service as cs
    from apps.rag_core.core import facade as core_facade

    def _forbidden_retrieval(**kw):  # pragma: no cover
        raise AssertionError("la garde doit intercepter AVANT tout retrieval")

    def _forbidden_llm(**kw):  # pragma: no cover
        raise AssertionError("la garde doit intercepter AVANT tout appel LLM")

    # Le chat passe par la façade Core : c'est elle que la garde doit précéder.
    monkeypatch.setattr(core_facade, "query_rag", _forbidden_retrieval)
    monkeypatch.setattr(cs, "generate_text", _forbidden_llm)
    monkeypatch.setattr(cs, "llm_route_news_or_none", _forbidden_llm)


@pytest.fixture
def stub_rag(monkeypatch):
    """RAG normal, neutralisé — pour les questions qui DOIVENT passer."""
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
        "status": "ok", "sources": [EXCEL_CHUNK], "prompt": "P",
        "log_id": None, "answer_kind": "ok",
    })
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "Le budget d'Atlas est de 250000. [1]",
        "provider": "stub", "model": "m", "usage": {},
    })
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)


def _ask(api_client, rag_space, q, thread_id=None):
    body = {"rag_space_id": str(rag_space.id), "question": q}
    if thread_id:
        body["thread_id"] = str(thread_id)
    return api_client.post("/api/v1/chat/", data=body, format="json")


def _stream(api_client, rag_space, q):
    res = api_client.post(
        "/api/v1/chat/stream/",
        data={"rag_space_id": str(rag_space.id), "question": q},
        format="json",
    )
    body = b"".join(res.streaming_content).decode()
    events = {}
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name = next((ln[len("event:"):].strip() for ln in block.splitlines()
                     if ln.startswith("event:")), None)
        data = next((ln[len("data:"):].strip() for ln in block.splitlines()
                     if ln.startswith("data:")), None)
        if name and data:
            events.setdefault(name, []).append(json.loads(data))
    return res, events


# --------------------------------------------------------------------------- #
# La garde intercepte, et n'appelle RIEN
# --------------------------------------------------------------------------- #
class TestGuardBlocks:
    def test_analytic_question_is_intercepted_before_anything(
        self, api_client, rag_space, no_llm_no_retrieval
    ):
        res = _ask(api_client, rag_space, BLOCKED_Q)

        assert res.status_code == 200, res.data
        assert res.data["answer_kind"] == ANSWER_KIND
        assert res.data["status"] == "ok"
        assert res.data["sources"] == []
        assert res.data["log_id"] is None
        assert res.data["analytics"] == {"requested_intent": "grouping", "available": False}

        # Le message dit POURQUOI, pas seulement « non ».
        assert "calcul exhaustif" in res.data["answer"]
        assert "pas encore disponible" in res.data["answer"]

    def test_no_documentary_query_log_is_created(
        self, api_client, rag_space, no_llm_no_retrieval
    ):
        """Aucun retrieval n'a eu lieu : il n'y a rien à auditer comme recherche."""
        before = RagQueryLog.objects.count()
        _ask(api_client, rag_space, BLOCKED_Q)
        assert RagQueryLog.objects.count() == before

    def test_an_action_audit_event_is_emitted(self, api_client, rag_space, no_llm_no_retrieval):
        from apps.audit.models import ActionAuditEvent

        _ask(api_client, rag_space, BLOCKED_Q)
        ev = ActionAuditEvent.objects.filter(action_name="excel.analytics_blocked").latest("created_at")
        assert ev.metadata["intent"] == "grouping"
        assert ev.metadata["reason"] == "grouping_by_dimension"
        # Aucune cellule, aucune source, aucun contenu de question.
        assert "question" not in ev.metadata
        assert "sources" not in ev.metadata

    def test_two_safe_rephrasings_are_offered(self, api_client, rag_space, no_llm_no_retrieval):
        res = _ask(api_client, rag_space, BLOCKED_Q)
        sugg = res.data["suggested_rephrasings"]
        assert len(sugg) == 2
        joined = " ".join(sugg).lower()
        for forbidden in ("calcule", "somme", "moyenne", "regroupe", "top "):
            assert forbidden not in joined


class TestGuardLetsDocumentaryQuestionsThrough:
    def test_local_question_still_goes_to_the_rag(self, api_client, rag_space, stub_rag):
        res = _ask(api_client, rag_space, LOCAL_Q)
        assert res.status_code == 200
        assert res.data["answer_kind"] != ANSWER_KIND
        assert res.data["sources"]                      # le RAG a bien tourné
        assert res.data["sources"][0]["meta"]["sheet_name"] == "Synthèse"

    def test_non_excel_analytic_question_is_not_blocked(self, api_client, rag_space, stub_rag):
        """Un `.xlsx` ailleurs dans l'espace ne doit pas faire basculer une question générale."""
        res = _ask(api_client, rag_space, "Quelle est la moyenne nationale du chômage ?")
        assert res.data["answer_kind"] != ANSWER_KIND


class TestThreadContext:
    def test_implicit_workbook_from_the_previous_turn(self, api_client, rag_space, stub_rag,
                                                      monkeypatch):
        """« Le budget total par direction ? » posé APRÈS une réponse issue d'un classeur.

        La question ne nomme aucun classeur : c'est le FIL qui le sait.
        """
        first = _ask(api_client, rag_space, LOCAL_Q)
        thread_id = first.data["thread_id"]

        # Le tour précédent a bien cité un classeur.
        assert first.data["sources"][0]["meta"]["kind"] == "excel_table"

        import apps.chat.services.chat_service as cs

        def _forbidden(**kw):  # pragma: no cover
            raise AssertionError("la garde devait intercepter grâce au contexte du fil")

        monkeypatch.setattr(cs, "_dispatch_retrieval", _forbidden)
        monkeypatch.setattr(cs, "generate_text", _forbidden)
        monkeypatch.setattr(cs, "llm_route_news_or_none", _forbidden)

        res = _ask(api_client, rag_space, "Quel est le budget total par direction ?",
                   thread_id=thread_id)
        assert res.data["answer_kind"] == ANSWER_KIND


# --------------------------------------------------------------------------- #
# Parité sync / SSE, routeur ON / OFF
# --------------------------------------------------------------------------- #
class TestParity:
    def test_sse_matches_sync(self, api_client, rag_space, no_llm_no_retrieval):
        sync = _ask(api_client, rag_space, BLOCKED_Q).data
        res, events = _stream(api_client, rag_space, BLOCKED_Q)

        assert res.status_code == 200
        # Contrat SSE INCHANGÉ : meta -> sources -> token -> done. Aucun événement dédié.
        assert set(events) == {"meta", "sources", "token", "done"}

        assert events["meta"][0]["answer_kind"] == ANSWER_KIND
        assert events["sources"][0]["sources"] == []
        assert events["token"][0]["delta"] == sync["answer"]

        done = events["done"][0]
        assert done["answer_kind"] == sync["answer_kind"]
        assert done["status"] == sync["status"]
        assert done["log_id"] is None
        assert done["suggested_rephrasings"] == sync["suggested_rephrasings"]
        assert done["analytics"] == sync["analytics"]

    @pytest.mark.parametrize("router_on", [False, True])
    def test_router_on_and_off_are_equivalent(self, api_client, rag_space, settings,
                                              no_llm_no_retrieval, router_on):
        """La garde est dans la VUE, en amont du routeur : ON et OFF donnent le même résultat."""
        settings.ACTIONS_ROUTER_ENABLED = router_on
        res = _ask(api_client, rag_space, BLOCKED_Q)
        assert res.data["answer_kind"] == ANSWER_KIND
        assert res.data["sources"] == []


# --------------------------------------------------------------------------- #
# /help passe AVANT la garde
# --------------------------------------------------------------------------- #
class TestInterceptionOrder:
    def test_help_is_never_classified_as_an_excel_question(
        self, api_client, rag_space, no_llm_no_retrieval
    ):
        res = _ask(api_client, rag_space, "/help limites")
        assert res.data["answer_kind"] == "help"
        assert res.data["answer_kind"] != ANSWER_KIND

        answer = res.data["answer"]
        # L'aide dit bien la limite Excel — SANS jamais employer le mot « exhaustif »,
        # qu'un garde-fou de longue date bannit du markdown d'aide (HELP4) : il
        # empêche toute PROMESSE d'exhaustivité, et il a raison de rester.
        assert "tout un classeur" in answer
        assert "exhaustif" not in answer.lower()


# --------------------------------------------------------------------------- #
# F5
# --------------------------------------------------------------------------- #
class TestHistory:
    def test_the_refusal_survives_a_reload_without_reclassifying(
        self, api_client, rag_space, no_llm_no_retrieval
    ):
        res = _ask(api_client, rag_space, BLOCKED_Q)
        thread_id = res.data["thread_id"]

        # Persisté comme une réponse assistant ordinaire.
        msg = ChatMessage.objects.filter(thread_id=thread_id, role="assistant").latest("created_at")
        assert "calcul exhaustif" in msg.content
        assert msg.sources in ([], None)
        assert msg.rag_query_log_id is None

        # Rechargement : aucun LLM, aucun retrieval (les sentinelles sont toujours en place).
        reload = api_client.get(f"/api/v1/chat/threads/{thread_id}/messages/")
        assert reload.status_code == 200
        assistant = [m for m in reload.data if m["role"] == "assistant"][-1]
        assert "calcul exhaustif" in assistant["content"]


# --------------------------------------------------------------------------- #
# P1.2 — totaux et énumérations exhaustives
# --------------------------------------------------------------------------- #
TOTAL_Q = "Quel est le total des shares outstanding de ce classeur ?"
LISTING_Q = "Liste toutes les lignes du classeur avec leurs valeurs."


class TestTotalsAndListingsAreGuardedToo:
    """Les deux questions qui ont produit une réponse fausse en production.

    L'une a fait passer la ligne « Common » pour le total de l'entreprise ; l'autre a
    rendu deux lignes sur onze sans dire qu'elle était partielle. Toutes deux doivent
    désormais être arrêtées AVANT le retrieval et AVANT le LLM.
    """

    def test_a_total_request_is_intercepted_before_anything(
        self, api_client, rag_space, no_llm_no_retrieval
    ):
        res = _ask(api_client, rag_space, TOTAL_Q)

        assert res.status_code == 200, res.data
        assert res.data["answer_kind"] == ANSWER_KIND
        assert res.data["sources"] == []
        assert res.data["log_id"] is None
        assert res.data["analytics"]["requested_intent"] == "exhaustive_aggregation"
        # Le refus nomme le danger RÉEL : un chiffre de catégorie pris pour un total.
        assert "faux" in res.data["answer"].lower()

    def test_an_exhaustive_listing_is_intercepted_before_anything(
        self, api_client, rag_space, no_llm_no_retrieval
    ):
        res = _ask(api_client, rag_space, LISTING_Q)

        assert res.status_code == 200, res.data
        assert res.data["answer_kind"] == ANSWER_KIND
        assert res.data["analytics"]["requested_intent"] == "exhaustive_enumeration"
        # Le refus admet qu'il pourrait OMETTRE des lignes — c'est tout l'aveu.
        assert "exhaustive" in res.data["answer"].lower()

    @pytest.mark.parametrize("q", [TOTAL_Q, LISTING_Q])
    def test_no_documentary_query_log_is_created(
        self, api_client, rag_space, no_llm_no_retrieval, q
    ):
        before = RagQueryLog.objects.count()
        _ask(api_client, rag_space, q)
        assert RagQueryLog.objects.count() == before

    @pytest.mark.parametrize("q", [TOTAL_Q, LISTING_Q])
    def test_the_audit_records_the_intent_without_leaking_the_table(
        self, api_client, rag_space, no_llm_no_retrieval, q
    ):
        from apps.audit.models import ActionAuditEvent

        _ask(api_client, rag_space, q)
        ev = ActionAuditEvent.objects.filter(
            action_name="excel.analytics_blocked"
        ).latest("created_at")
        assert ev.metadata["intent"] in ("exhaustive_aggregation", "exhaustive_enumeration")
        assert ev.metadata["reason"].startswith("excel_")
        assert "question" not in ev.metadata
        assert "sources" not in ev.metadata

    @pytest.mark.parametrize("q", [TOTAL_Q, LISTING_Q])
    def test_sse_and_sync_agree(self, api_client, rag_space, no_llm_no_retrieval, q):
        """Le contrat SSE ne doit pas offrir de porte dérobée à une question bloquée."""
        res, events = _stream(api_client, rag_space, q)
        assert res.status_code == 200
        assert events["meta"][0]["answer_kind"] == ANSWER_KIND
        assert events["sources"][0]["sources"] == []
        assert "done" in events

    def test_reading_the_total_row_still_reaches_the_rag(self, api_client, rag_space, stub_rag):
        """La lecture d'une ligne écrite reste documentaire — la garde reste utilisable."""
        res = _ask(
            api_client, rag_space,
            "Quelle valeur est indiquée sur la ligne SUBTOTAL de la feuille ?",
        )
        assert res.status_code == 200
        assert res.data.get("answer_kind") != ANSWER_KIND
