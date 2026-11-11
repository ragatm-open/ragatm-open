"""Parité flag OFF (historique) vs flag ON (routeur) — sync et SSE (audit §12, §15).

On exerce directement le code que les vues appellent :
- flag ON  = ``wiring.run_chat_sync`` / ``wiring.run_chat_stream``
- flag OFF = ``chat_service.chat_query`` / ``chat_query_stream`` (+ helpers listing)

Les textes LLM sont mockés ; on vérifie AUSSI le nombre d'appels LLM/retrieval.
"""
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


def _vd(question, thread_id=None):
    return {"rag_space_id": RS, "question": question, "thread_id": thread_id}


@pytest.fixture
def counters(monkeypatch):
    c = {"llm": 0, "retrieval": 0}
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)

    def fake_retrieval(**k):
        c["retrieval"] += 1
        return {
            "status": "ok",
            "sources": [{"chunk_id": "c1", "text": "t", "score": 1.0, "source": {}}],
            "prompt": "PROMPT",
            "log_id": None,
            "answer_kind": "ok",
        }

    monkeypatch.setattr(cs, "_run_retrieval", fake_retrieval)

    def fake_generate(**k):
        c["llm"] += 1
        return {"text": "Réponse.", "provider": "stub", "model": "m", "usage": None, "tokens": None}

    monkeypatch.setattr(cs, "generate_text", fake_generate)

    # Couture STREAMING : ``chat_query_stream`` n'appelle PAS ``generate_text`` quand la
    # Gateway est désactivée (cas des tests, cf. conftest AI_GATEWAY_ENABLED=0) — il
    # appelle ``generate_text_stream``. Sans ce mock, le test tapait un vrai Ollama s'il
    # se trouvait joignable (poste de dev) : deux générations réelles successives ne
    # produisent pas le même nombre de tokens => parité en échec, de façon
    # environnement-dépendante (vert en CI sans Ollama, rouge sur un poste GPU).
    # Le mock reflète le contrat consommé par chat_query_stream : événements ``delta``
    # puis ``result`` (provider/model/usage).
    def fake_generate_stream(**k):
        c["llm"] += 1
        yield {"type": "delta", "delta": "Réponse."}
        yield {"type": "result", "provider": "stub", "model": "m", "usage": None}

    monkeypatch.setattr(cs, "generate_text_stream", fake_generate_stream)
    return c


def _reset(c):
    c["llm"] = 0
    c["retrieval"] = 0


# ---- search.answer : parité sync + comptage identique ---------------------
def test_answer_parity_sync(counters):
    vd = _vd("Quelle est la durée du contrat ?")

    hist = cs.chat_query(user=None, rag_space=None, **vd)
    hist_calls = dict(counters)
    _reset(counters)

    routed = wiring.run_chat_sync(request=DummyReq(), user=None, rag_space=None, validated_data=vd)

    assert routed == hist
    assert counters == hist_calls == {"llm": 1, "retrieval": 1}


def test_answer_parity_stream(counters):
    vd = _vd("Quels sont les risques principaux ?")

    hist = list(cs.chat_query_stream(user=None, rag_space=None, request_id="r", **vd))
    hist_calls = dict(counters)
    _reset(counters)

    routed = list(
        wiring.run_chat_stream(request=DummyReq(), user=None, rag_space=None, request_id="r", validated_data=vd)
    )

    assert [e["event"] for e in routed] == [e["event"] for e in hist]
    assert routed == hist
    # Comptage EXPLICITE (et pas seulement `counters == hist_calls`) : c'est ce qui
    # garantit que la couture streaming mockée a bien été empruntée. Sans cette
    # assertion, le test passait « à vide » là où aucun LLM n'est joignable (CI) —
    # les deux chemins échouant identiquement — et échouait là où un Ollama réel
    # répondait. Un 0 ici = le vrai `generate_text_stream` a été appelé.
    assert counters == hist_calls == {"llm": 1, "retrieval": 1}

    # Contrat SSE : meta -> sources -> token(s) -> done, et le texte n'est jamais dupliqué.
    assert [e["event"] for e in routed] == ["meta", "sources", "token", "done"]
    assert "".join(e["data"]["delta"] for e in routed if e["event"] == "token") == "Réponse."
    assert routed[-1]["data"]["status"] == "ok"


# ---- no_answer : parité --------------------------------------------------
def test_no_answer_parity_sync(monkeypatch):
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval", lambda **k: {"status": "no_answer", "sources": [], "log_id": None}
    )
    calls = {"llm": 0}
    monkeypatch.setattr(cs, "generate_text", lambda **k: calls.__setitem__("llm", calls["llm"] + 1) or {"text": ""})

    vd = _vd("Question totalement absente des documents ?")
    hist = cs.chat_query(user=None, rag_space=None, **vd)
    routed = wiring.run_chat_sync(request=DummyReq(), user=None, rag_space=None, validated_data=vd)

    assert routed == hist
    assert routed["status"] == "no_answer"
    assert calls["llm"] == 0  # aucune génération sur no_answer (parité)


def test_no_answer_parity_stream(monkeypatch):
    """Pendant SSE de ``test_no_answer_parity_sync`` : le no-answer ne doit générer
    aucun token LLM, et le chemin routé doit être identique à l'historique.

    Les DEUX coutures de génération sont mockées : `chat_query_stream` emprunte
    ``generate_text_stream`` (Gateway OFF) et non ``generate_text`` — ne mocker que la
    seconde laisserait fuiter un appel LLM réel.
    """
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval", lambda **k: {"status": "no_answer", "sources": [], "log_id": None}
    )
    calls = {"llm": 0}

    def _boom_sync(**k):
        calls["llm"] += 1
        return {"text": ""}

    def _boom_stream(**k):
        calls["llm"] += 1
        yield {"type": "result", "provider": "stub", "model": "m", "usage": None}

    monkeypatch.setattr(cs, "generate_text", _boom_sync)
    monkeypatch.setattr(cs, "generate_text_stream", _boom_stream)

    vd = _vd("Question totalement absente des documents ?")
    hist = list(cs.chat_query_stream(user=None, rag_space=None, request_id="r", **vd))
    routed = list(
        wiring.run_chat_stream(
            request=DummyReq(), user=None, rag_space=None, request_id="r", validated_data=vd
        )
    )

    assert [e["event"] for e in routed] == [e["event"] for e in hist]
    assert routed == hist
    assert routed[-1]["event"] == "done"
    assert routed[-1]["data"]["status"] == "no_answer"
    assert calls["llm"] == 0  # aucune génération sur no_answer, y compris en streaming


# ---- listing : parité de forme par transport -----------------------------
def _patch_listing(monkeypatch):
    from api.v1.chat import views as cv

    monkeypatch.setattr(cv, "_extract_term_for_listing", lambda q: "Nordexia")
    monkeypatch.setattr(cv, "_run_listing_search", lambda **k: ["hit"])
    monkeypatch.setattr(cv, "_format_listing_answer", lambda term, hits: f"Docs {term}")
    return cv


def test_listing_sync_shape(counters, monkeypatch):
    _patch_listing(monkeypatch)
    out = wiring.run_chat_sync(
        request=DummyReq(), user=None, rag_space=None, validated_data=_vd("Liste les articles sur Nordexia")
    )
    # Parité EXACTE avec le sync historique : ni thread, ni log.
    assert out == {"answer": "Docs Nordexia", "sources": [], "log_id": None}
    assert counters == {"llm": 0, "retrieval": 0}


def test_listing_stream_shape(counters, monkeypatch):
    cv = _patch_listing(monkeypatch)

    class _T:
        id = "tid-123"

    monkeypatch.setattr(cv, "_ensure_thread_and_persist_messages", lambda **k: _T())

    events = list(
        wiring.run_chat_stream(
            request=DummyReq(), user=None, rag_space=None, request_id="r",
            validated_data=_vd("Quels documents parlent de Nordexia ?"),
        )
    )
    assert [e["event"] for e in events] == ["meta", "sources", "token", "done"]
    assert events[0]["data"]["thread_id"] == "tid-123"  # SSE crée/persiste un thread
    assert events[2]["data"]["delta"] == "Docs Nordexia"
    assert events[3]["data"]["status"] == "ok"
    assert counters == {"llm": 0, "retrieval": 0}
