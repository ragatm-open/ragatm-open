"""La garde analytique doit s'armer AUSSI au premier tour d'un fil neuf.

Le défaut, mesuré en campagne terrain. La garde pré-retrieval exige de SAVOIR que la
question porte sur un classeur — par un mot de tableur, ou par un tour précédent qui en
citait un. Au premier tour d'une conversation neuve, elle ne sait rien :

    « Quel est le total des shares outstanding pour AirWorks ? »   -> PASSE
    « Quel est le total des shares outstanding de ce classeur ? »  -> bloquée

Un seul mot séparait les deux. La première partait au LLM, qui répondait par la valeur
d'une ligne de catégorie présentée comme le total de l'entreprise.

Le retrieval, lui, SAIT : il vient de retrouver un classeur. On décide donc une seconde
fois, après lui et avant le modèle. Payer une recherche pour ne pas payer un chiffre
faux est le bon échange — le retrieval ne fabrique rien, seule la génération ment.
"""
import json

import pytest

from apps.audit.models import RagQueryLog
from apps.chat.services.excel_guard import ANSWER_KIND

pytestmark = pytest.mark.django_db

#: Aucun mot de tableur. C'est tout l'intérêt du cas.
BLOCKED_TOTAL = "Quel est le total des shares outstanding pour AirWorks ?"
BLOCKED_LIST = "Donne toutes les valeurs de la colonne Shares Outstanding."
LOCAL_READ = "Quelle valeur est indiquée sur la ligne SUBTOTAL ?"

_EXCEL_SOURCE = {
    "chunk_id": "c1",
    "text": "| Common | 6234211 |",
    "score": 0.9,
    "source": {
        "document_id": "d1",
        "filename": "Pro Forma.xlsx",
        "meta": {"kind": "excel_table", "sheet_name": "Summary Pro Forma",
                 "cell_range": "A3:S13", "headers": ["Security", "Shares Outstanding"]},
    },
}
_PDF_SOURCE = {
    "chunk_id": "c2",
    "text": "Le marché du capital-investissement…",
    "score": 0.8,
    "source": {"document_id": "d2", "filename": "revue.pdf", "meta": {}},
}


def _retrieval(sources):
    return {
        "status": "ok",
        "sources": list(sources),
        "prompt": "PROMPT",
        "log_id": None,
        "answer_kind": "ok",
    }


@pytest.fixture
def excel_retrieval(monkeypatch):
    """Le retrieval retrouve un classeur ; TOUT appel LLM lève."""
    import apps.chat.services.chat_service as cs

    def _forbidden_llm(*a, **kw):
        raise AssertionError("le LLM ne doit JAMAIS être appelé quand la garde s'arme")

    monkeypatch.setattr(cs, "_dispatch_retrieval",
                        lambda **kw: _retrieval([_PDF_SOURCE, _EXCEL_SOURCE]))
    monkeypatch.setattr(cs, "generate_text", _forbidden_llm)
    monkeypatch.setattr(cs, "generate_text_stream", _forbidden_llm)
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)


@pytest.fixture
def pdf_only_retrieval(monkeypatch):
    """Aucune source Excel : la garde ne doit PAS s'armer."""
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: _retrieval([_PDF_SOURCE]))
    monkeypatch.setattr(cs, "generate_text",
                        lambda **kw: {"text": "Le total indiqué est 42.", "provider": "stub"})
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)


def _ask(api_client, rag_space, q):
    return api_client.post(
        "/api/v1/chat/",
        data={"rag_space_id": str(rag_space.id), "question": q},
        format="json",
    )


def _stream(api_client, rag_space, q):
    res = api_client.post(
        "/api/v1/chat/stream/",
        data={"rag_space_id": str(rag_space.id), "question": q},
        format="json",
    )
    body = b"".join(res.streaming_content).decode()
    events = {}
    for block in body.split("\n\n"):
        name = next((ln[6:].strip() for ln in block.splitlines() if ln.startswith("event:")), None)
        data = next((ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")), None)
        if name and data:
            events.setdefault(name, []).append(json.loads(data))
    return res, events


class TestTheFirstTurnOfAFreshThread:
    @pytest.mark.parametrize("q", [BLOCKED_TOTAL, BLOCKED_LIST])
    def test_an_analytical_question_is_blocked_after_retrieval(
        self, api_client, rag_space, excel_retrieval, q
    ):
        res = _ask(api_client, rag_space, q)

        assert res.status_code == 200, res.data
        assert res.data["answer_kind"] == ANSWER_KIND
        assert res.data["analytics"]["available"] is False
        # Le LLM aurait levé : ne pas être ici prouve qu'il n'a pas été appelé.

    def test_the_retrieved_sources_are_still_shown(
        self, api_client, rag_space, excel_retrieval
    ):
        """Le refus est crédible PARCE QU'on montre ce qu'on a trouvé.

        Ce qu'on ne fait pas, c'est RÉPONDRE à partir de ces extraits.
        """
        res = _ask(api_client, rag_space, BLOCKED_TOTAL)
        assert res.data["sources"], "les sources retrouvées restent visibles"
        assert any(s["filename"].endswith(".xlsx") for s in res.data["sources"])

    def test_two_safe_rephrasings_are_offered(self, api_client, rag_space, excel_retrieval):
        res = _ask(api_client, rag_space, BLOCKED_TOTAL)
        assert len(res.data["suggested_rephrasings"]) == 2

    def test_the_refusal_is_persisted_and_survives_f5(
        self, api_client, rag_space, excel_retrieval
    ):
        from apps.chat.models import ChatMessage

        res = _ask(api_client, rag_space, BLOCKED_TOTAL)
        thread_id = res.data["thread_id"]

        msg = ChatMessage.objects.filter(thread_id=thread_id, role="assistant").latest("created_at")
        assert msg.content == res.data["answer"]
        assert msg.sources, "les sources citées survivent au rechargement"


class TestSyncAndSseAgree:
    @pytest.mark.parametrize("q", [BLOCKED_TOTAL, BLOCKED_LIST])
    def test_no_token_is_emitted_before_the_decision(
        self, api_client, rag_space, excel_retrieval, q
    ):
        res, events = _stream(api_client, rag_space, q)

        assert res.status_code == 200
        assert "token" not in events, "aucun token ne doit être émis"
        assert events["done"][0]["answer_kind"] == ANSWER_KIND
        assert "done" in events

    def test_the_final_text_is_identical_in_both_modes(
        self, api_client, rag_space, excel_retrieval
    ):
        sync = _ask(api_client, rag_space, BLOCKED_TOTAL).data["answer"]
        _, events = _stream(api_client, rag_space, BLOCKED_TOTAL)
        assert events["done"][0]["answer"] == sync


class TestTheGuardStaysUsable:
    def test_a_local_read_still_reaches_the_llm(
        self, api_client, rag_space, monkeypatch
    ):
        import apps.chat.services.chat_service as cs

        monkeypatch.setattr(cs, "_dispatch_retrieval",
                            lambda **kw: _retrieval([_EXCEL_SOURCE]))
        monkeypatch.setattr(cs, "generate_text",
                            lambda **kw: {"text": "23 379 395.", "provider": "stub"})
        monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

        res = _ask(api_client, rag_space, LOCAL_READ)
        assert res.data.get("answer_kind") != ANSWER_KIND

    def test_an_analytical_question_without_any_excel_source_is_not_blocked(
        self, api_client, rag_space, pdf_only_retrieval
    ):
        """Condition B : sans classeur retrouvé, la garde Excel n'a rien à dire."""
        res = _ask(api_client, rag_space, "Quel est le total indiqué dans ce contrat ?")
        assert res.data.get("answer_kind") != ANSWER_KIND

    def test_a_mixed_top_k_still_arms_the_guard(
        self, api_client, rag_space, excel_retrieval
    ):
        """UNE seule source Excel suffit — exiger l'unanimité désarmerait la garde.

        Le top-k d'un classeur noyé dans un espace de revues de presse contient
        TOUJOURS des PDF parasites : c'est le cas nominal, pas le cas limite.
        """
        res = _ask(api_client, rag_space, BLOCKED_TOTAL)
        assert res.data["answer_kind"] == ANSWER_KIND


class TestTheAuditTellsTheTruth:
    def test_a_query_log_may_exist_because_a_search_really_happened(
        self, api_client, rag_space, user, monkeypatch
    ):
        """Différence ASSUMÉE avec la garde pré-retrieval.

        Pré-retrieval : aucune recherche -> aucun log documentaire.
        Post-retrieval : une recherche a EU LIEU -> l'effacer falsifierait l'audit.
        """
        import apps.chat.services.chat_service as cs

        log = RagQueryLog.objects.create(
            rag_space=rag_space, workspace=rag_space.workspace, user=user,
            question=BLOCKED_TOTAL, status=RagQueryLog.STATUS_OK,
        )

        def _forbidden(*a, **kw):
            raise AssertionError("aucun LLM")

        monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
            **_retrieval([_EXCEL_SOURCE]), "log_id": str(log.id),
        })
        monkeypatch.setattr(cs, "generate_text", _forbidden)
        monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)

        res = _ask(api_client, rag_space, BLOCKED_TOTAL)
        assert res.data["log_id"] == str(log.id)

        log.refresh_from_db()
        assert log.policy == "excel_analytics_post_retrieval"
        assert log.answer == res.data["answer"]
