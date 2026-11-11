"""Tests du correctif streaming chat quand l'AI Gateway est active.

Gateway active => pseudo-stream via ``generate_text`` (non-streaming, presets +
routage + think + retry), au lieu de ``generate_text_stream`` qui lève LLMError.
Gateway inactive => vrai streaming conservé. Mocks uniquement.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.conf import settings

from apps.chat.services import chat_service as cs
from apps.llm_router.router import LLMError


def _patch_common(monkeypatch, *, sources=None):
    monkeypatch.setattr(cs, "normalize_llm_overrides", lambda space: ("ollama", "llama3.1:8b"))
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(cs, "mode_b_enabled", lambda: False)
    monkeypatch.setattr(cs, "_conversation_context_payload", lambda **k: "")
    monkeypatch.setattr(cs, "maybe_update_log", lambda *a, **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: {"status": "ok", "prompt": "PROMPT", "sources": sources or [],
                     "log_id": None, "answer_kind": "ok"},
    )


def _events(**kw):
    rag_space = SimpleNamespace(llm_provider="ollama", llm_model="llama3.1:8b", allow_external_fallback=False)
    return list(cs.chat_query_stream(rag_space_id="rs", question="explique Tessaly", rag_space=rag_space, **kw))


@pytest.mark.django_db
def test_gateway_enabled_uses_non_streaming_pseudo_stream(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cs, "_gateway_enabled", lambda: True)

    captured = {}

    def fake_generate_text(**kw):
        captured.update(kw)
        return {"text": "Tessaly est une coopérative.", "provider": "ai_gateway", "model": "llama3.1:8b", "usage": None, "tokens": 4}

    def fake_stream(**kw):
        raise AssertionError("generate_text_stream ne doit PAS être appelé quand la Gateway est active")

    monkeypatch.setattr(cs, "generate_text", fake_generate_text)
    monkeypatch.setattr(cs, "generate_text_stream", fake_stream)

    events = _events()
    kinds = [e["event"] for e in events]
    assert "error" not in kinds
    token = next(e for e in events if e["event"] == "token")
    assert token["data"]["delta"] == "Tessaly est une coopérative."
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["status"] == "ok"
    assert "Tessaly est une coopérative." in done["data"]["answer"]
    # presets factuels transmis
    assert captured["options"] == {
        "temperature": settings.RAG_FACTUAL_TEMPERATURE,
        "num_ctx": settings.RAG_FACTUAL_NUM_CTX,
        "num_predict": settings.RAG_FACTUAL_NUM_PREDICT,
    }


@pytest.mark.django_db
def test_gateway_enabled_routes_model(monkeypatch, settings):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cs, "_gateway_enabled", lambda: True)
    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_FACTUAL = "mistral-small3.2:24b"

    captured = {}
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **kw: captured.update(kw) or {"text": "ok", "provider": "ai_gateway", "model": kw["model"], "usage": None, "tokens": 1},
    )
    _events()
    assert captured["model"] == "mistral-small3.2:24b"
    # Lot 1 : contexte d'egress résolu pour l'espace (remplace space_provider).
    assert captured["context"].external_egress_allowed is False


@pytest.mark.django_db
def test_gateway_disabled_keeps_real_streaming(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cs, "_gateway_enabled", lambda: False)

    def fake_stream(**kw):
        yield {"type": "delta", "delta": "Hello "}
        yield {"type": "delta", "delta": "world"}
        yield {"type": "result", "provider": "ollama", "model": "llama3.1:8b", "usage": None}

    monkeypatch.setattr(cs, "generate_text_stream", fake_stream)
    monkeypatch.setattr(cs, "generate_text", lambda **k: (_ for _ in ()).throw(AssertionError("stream disabled path must use generate_text_stream")))

    events = _events()
    deltas = [e["data"]["delta"] for e in events if e["event"] == "token"]
    assert deltas == ["Hello ", "world"]
    assert next(e for e in events if e["event"] == "done")["data"]["status"] == "ok"


@pytest.mark.django_db
def test_gateway_enabled_empty_after_retry_yields_error(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cs, "_gateway_enabled", lambda: True)

    def raising(**kw):
        raise LLMError("AI Gateway returned an empty response after one retry")

    monkeypatch.setattr(cs, "generate_text", raising)
    events = _events()
    kinds = [e["event"] for e in events]
    assert "error" in kinds
    err = next(e for e in events if e["event"] == "error")
    assert err["data"]["detail"] == "llm_error"
