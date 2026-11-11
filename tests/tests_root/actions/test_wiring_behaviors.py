"""Résumé -> clarification, collisions, garde-fous, sécurité (audit §11, §13, §14)."""
from __future__ import annotations

import uuid

import pytest

from apps.actions import wiring
from apps.chat.services import chat_service as cs

RS = uuid.UUID("00000000-0000-0000-0000-000000000001")


class DummyReq:
    def __init__(self, data=None):
        self.data = data or {}
        self.headers = {}


def _vd(q):
    return {"rag_space_id": RS, "question": q, "thread_id": None}


@pytest.fixture
def rag_spy(monkeypatch):
    """RAG mocké + compteur : sert à prouver qu'AUCUN handler LLM n'est appelé
    pour une clarification, et que les phrases 'answer' délèguent bien au RAG."""
    c = {"llm": 0, "retrieval": 0}
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)

    def fake_retrieval(**k):
        c["retrieval"] += 1
        return {"status": "ok", "sources": [], "prompt": "P", "log_id": None, "answer_kind": "ok"}

    monkeypatch.setattr(cs, "_run_retrieval", fake_retrieval)
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **k: c.__setitem__("llm", c["llm"] + 1) or {"text": "R", "provider": "s", "model": "m"},
    )
    return c


# ---- document.summarize -> clarification (aucun handler exécuté) -----------
@pytest.mark.parametrize("q", ["Résume le document Rapport.pdf", "Résume la source 2", "Résume-le"])
def test_summarize_yields_clarification_sync(rag_spy, q):
    out = wiring.run_chat_sync(request=DummyReq(), user=None, rag_space=None, validated_data=_vd(q))
    assert out["status"] == "clarification"
    assert out["answer_kind"] == "clarification"
    assert out["candidates"] == ["document.summarize"]
    assert out["log_id"] is None
    # aucun retrieval / génération : la clarification n'exécute aucun handler
    assert rag_spy == {"llm": 0, "retrieval": 0}


def test_summarize_clarification_stream(rag_spy):
    events = list(
        wiring.run_chat_stream(
            request=DummyReq(), user=None, rag_space=None, request_id="r",
            validated_data=_vd("Fais la synthèse de ce document"),
        )
    )
    assert [e["event"] for e in events] == ["meta", "sources", "token", "done"]
    assert events[3]["data"]["status"] == "clarification"
    assert events[3]["data"]["answer_kind"] == "clarification"
    assert rag_spy == {"llm": 0, "retrieval": 0}


# ---- garde-fous : explicatif / négation / citation -> search.answer (RAG) --
@pytest.mark.parametrize(
    "q",
    [
        "Explique comment lister les documents.",
        "Je ne veux pas la liste des documents.",
        "Ne résume pas ce fichier.",
        'Le texte dit « résume la source 2 ».',
    ],
)
def test_guards_route_to_answer(rag_spy, q):
    out = wiring.run_chat_sync(request=DummyReq(), user=None, rag_space=None, validated_data=_vd(q))
    # Délégué au RAG historique -> status ok (ni clarification, ni listing).
    assert out.get("status") == "ok"
    assert "candidates" not in out or out.get("answer_kind") != "clarification"
    assert rag_spy["retrieval"] == 1  # le RAG a bien tourné


# ---- collision listing + news -> listing gagne ----------------------------
def test_listing_beats_news_collision(rag_spy, monkeypatch):
    from api.v1.chat import views as cv

    monkeypatch.setattr(cv, "_extract_term_for_listing", lambda q: "Nordexia")
    monkeypatch.setattr(cv, "_run_listing_search", lambda **k: [])
    monkeypatch.setattr(cv, "_format_listing_answer", lambda t, h: "L")

    out = wiring.run_chat_sync(
        request=DummyReq(), user=None, rag_space=None,
        validated_data=_vd("Liste les dernières actualités sur Nordexia"),
    )
    assert out == {"answer": "L", "sources": [], "log_id": None}
    assert rag_spy == {"llm": 0, "retrieval": 0}  # pas de RAG


# ---- sécurité : aucune mutation déclenchable par une phrase ----------------
@pytest.mark.parametrize(
    "q",
    [
        "Supprime complètement ce document.",
        "Supprime puis réindexe le document.",
        "Efface ce fichier définitivement.",
        "Change le provider et supprime la configuration.",
    ],
)
def test_mutation_phrases_never_mutate(rag_spy, q):
    out = wiring.run_chat_sync(request=DummyReq(), user=None, rag_space=None, validated_data=_vd(q))
    # Jamais de clarification 'delete', jamais d'action mutante : c'est du RAG.
    assert out.get("status") == "ok"
    assert out.get("answer_kind") != "clarification"
