"""Lot E3 — les citations Excel survivent au trajet complet, jusqu'au F5.

Ce que ces tests vérifient, et que les tests unitaires ne peuvent pas voir :

1. la réponse SYNCHRONE porte feuille + plage ;
2. le flux SSE porte EXACTEMENT les mêmes informations (parité) ;
3. `ChatMessage.sources` les persiste ;
4. **le rechargement du fil (F5) les retrouve** — c'est la preuve qui compte, parce
   que rien ne garantissait que la persistance ne soit pas qu'un effet de bord du
   streaming en mémoire ;
5. les sources PDF ne changent pas d'un iota.
"""
import json

import pytest

from apps.chat.models import ChatMessage

pytestmark = pytest.mark.django_db

EXCEL_CHUNK = {
    "chunk_id": "c-excel-1",
    "document_id": "d-excel",
    "text": "Feuille : Synthèse (B8:F17)\n\n| Projet | Budget |\n| Atlas | 250000 |",
    "score": 0.93,
    "source": {
        "document_id": "d-excel",
        "filename": "Budget_2026.xlsx",
        "page_number": None,          # une feuille n'est PAS une page
        "meta": {
            "kind": "excel_table",
            "sheet_name": "Synthèse",
            "cell_range": "B8:F17",
            "row_start": 8,
            "row_end": 17,
            "title": "Synthèse budgétaire",
            "chunk_index": 4,          # bruit interne : ne doit pas ressortir
        },
    },
}

PDF_CHUNK = {
    "chunk_id": "c-pdf-1",
    "document_id": "d-pdf",
    "text": "Le rapport indique une hausse.",
    "score": 0.81,
    "source": {
        "document_id": "d-pdf",
        "filename": "Rapport.pdf",
        "page_number": 12,
        "meta": {"chunk_index": 2, "char_start": 0},
    },
}


@pytest.fixture
def stub_rag(monkeypatch):
    """Neutralise le retrieval et le LLM : on teste le TRANSPORT des sources."""
    import apps.chat.services.chat_service as cs

    monkeypatch.setattr(cs, "_dispatch_retrieval", lambda **kw: {
        "status": "ok",
        "sources": [EXCEL_CHUNK, PDF_CHUNK],
        "prompt": "P",
        "log_id": None,
        "answer_kind": "ok",
    })
    monkeypatch.setattr(cs, "generate_text", lambda **kw: {
        "text": "Le budget d'Atlas est de 250000. [1]",
        "provider": "stub", "model": "m", "usage": {},
    })
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **kw: None)


def _ask(api_client, rag_space, thread_id=None):
    body = {"rag_space_id": str(rag_space.id), "question": "Quel est le budget d'Atlas ?"}
    if thread_id:
        body["thread_id"] = str(thread_id)
    return api_client.post("/api/v1/chat/", data=body, format="json")


def _excel(sources):
    return next(s for s in sources if s["filename"] == "Budget_2026.xlsx")


def _pdf(sources):
    return next(s for s in sources if s["filename"] == "Rapport.pdf")


class TestSyncResponse:
    def test_excel_source_carries_sheet_and_range(self, api_client, rag_space, stub_rag):
        res = _ask(api_client, rag_space)
        assert res.status_code == 200, res.data

        s = _excel(res.data["sources"])
        assert s["schema_version"] == 3
        assert s["page_number"] is None                # jamais de fausse page
        assert s["meta"]["sheet_name"] == "Synthèse"
        assert s["meta"]["cell_range"] == "B8:F17"
        assert s["meta"]["title"] == "Synthèse budgétaire"
        assert "chunk_index" not in s["meta"]          # bruit interne écarté

    def test_pdf_source_is_untouched(self, api_client, rag_space, stub_rag):
        s = _pdf(_ask(api_client, rag_space).data["sources"])
        assert s["page_number"] == 12
        assert s["meta"] == {}


class TestStreamingParity:
    def test_sse_sources_equal_sync_sources(self, api_client, rag_space, stub_rag):
        """Un même événement `sources`, une même charge utile : pas d'événement Excel dédié."""
        sync = _ask(api_client, rag_space).data["sources"]

        res = api_client.post(
            "/api/v1/chat/stream/",
            data={"rag_space_id": str(rag_space.id), "question": "Quel est le budget d'Atlas ?"},
            format="json",
        )
        assert res.status_code == 200
        body = b"".join(res.streaming_content).decode()

        payload = None
        for block in body.split("\n\n"):
            if "event: sources" in block:
                line = next(ln for ln in block.splitlines() if ln.startswith("data:"))
                payload = json.loads(line[len("data:"):].strip())
                break
        assert payload is not None, body[:400]

        stream = payload["sources"]
        keys = ("document_id", "chunk_id", "filename", "page_number", "snippet", "meta")
        for a, b in zip(sync, stream, strict=True):
            for k in keys:
                assert a[k] == b[k], f"divergence sync/SSE sur `{k}`"


class TestHistoryF5:
    def test_sheet_and_range_survive_a_reload(self, api_client, rag_space, stub_rag):
        """LA preuve du lot : après un F5, la citation doit être identique.

        Elle ne doit rien devoir aux événements SSE gardés en mémoire par le client.
        """
        res = _ask(api_client, rag_space)
        thread_id = res.data["thread_id"]
        before = _excel(res.data["sources"])

        # Ce qui a été RÉELLEMENT persisté (pas ce qui a transité).
        msg = ChatMessage.objects.filter(thread_id=thread_id, role="assistant").latest("created_at")
        assert _excel(msg.sources)["meta"]["cell_range"] == "B8:F17"

        # Ce que le client reçoit en rechargeant le fil.
        reload = api_client.get(f"/api/v1/chat/threads/{thread_id}/messages/")
        assert reload.status_code == 200
        assistant = [m for m in reload.data if m["role"] == "assistant"][-1]

        after = _excel(assistant["sources"])
        assert after["meta"]["sheet_name"] == before["meta"]["sheet_name"] == "Synthèse"
        assert after["meta"]["cell_range"] == before["meta"]["cell_range"] == "B8:F17"
        assert after["page_number"] is None
        # Le PDF traverse le rechargement inchangé lui aussi.
        assert _pdf(assistant["sources"])["page_number"] == 12
