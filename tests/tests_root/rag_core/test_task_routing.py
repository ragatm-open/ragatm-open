"""Tests du routage OPT-IN du modèle par type de tâche.

Couvre le résolveur pur (activation, override vide, non-local, no-op) et le
câblage des trois appelants (chat factuel, résumé/rapport, réécriture) : le modèle
routé doit être transmis à ``generate_text`` (et à la décision ``think``). Mocks
uniquement — aucun appel réseau.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.llm_router import task_routing as tr


# --------------------------------------------------------------- résolveur pur

def test_disabled_by_default_no_change(settings):
    settings.RAG_TASK_ROUTING_ENABLED = False
    settings.RAG_TASK_MODEL_REPORT = "qwen3:30b-instruct"
    assert tr.resolve_model_for_task(tr.TASK_REPORT, provider="ollama", model="llama3.1:8b") == (
        "ollama", "llama3.1:8b"
    )


def test_enabled_overrides_local_model(settings):
    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_REPORT = "qwen3:30b-instruct"
    assert tr.resolve_model_for_task(tr.TASK_REPORT, provider="ollama", model="llama3.1:8b") == (
        "ollama", "qwen3:30b-instruct"
    )


def test_empty_override_keeps_workspace_model(settings):
    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_FACTUAL = ""
    assert tr.resolve_model_for_task(tr.TASK_FACTUAL, provider="ollama", model="llama3.1:8b") == (
        "ollama", "llama3.1:8b"
    )


def test_non_local_provider_never_rerouted(settings):
    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_FACTUAL = "mistral-small3.2:24b"
    # Espace OpenAI : jamais rerouté (pas de coût externe surprise).
    assert tr.resolve_model_for_task(tr.TASK_FACTUAL, provider="openai", model="gpt-4.1-mini") == (
        "openai", "gpt-4.1-mini"
    )


def test_provider_is_never_changed(settings):
    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_REWRITE = "qwen3:14b"
    provider, model = tr.resolve_model_for_task(tr.TASK_REWRITE, provider="ollama", model="llama3.1:8b")
    assert provider == "ollama" and model == "qwen3:14b"


def test_unknown_task_no_override(settings):
    settings.RAG_TASK_ROUTING_ENABLED = True
    assert tr.resolve_model_for_task("unknown", provider="ollama", model="llama3.1:8b") == (
        "ollama", "llama3.1:8b"
    )


# --------------------------------------------------------------- câblage rewrite

def test_rewrite_uses_routed_model(monkeypatch, settings):
    from apps.rag_core.services.query import rewrite

    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_REWRITE = "qwen3:14b"
    monkeypatch.setattr(rewrite, "REWRITE_PROVIDER", "ollama")
    monkeypatch.setattr(rewrite, "REWRITE_MODEL", "llama3.1:8b")

    captured = {}
    monkeypatch.setattr(rewrite, "generate_text", lambda **kw: captured.update(kw) or {"text": "req"})
    rewrite.rewrite_query_with_llm("explique le contrat")
    assert captured["model"] == "qwen3:14b"
    assert captured["provider"] == "ollama"
    assert captured["think"] is False  # qwen3:14b + extraction => routé ET think ciblé


# --------------------------------------------------------------- câblage résumé

def test_summary_uses_routed_report_model(monkeypatch, settings):
    from apps.rag_core.services import summary_service as ss

    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_REPORT = "qwen3:30b-instruct"
    monkeypatch.setattr(ss, "normalize_llm_overrides", lambda space: ("ollama", "llama3.1:8b"))
    monkeypatch.setattr(ss, "_build_prompt", lambda **k: "PROMPT")

    captured = {}
    monkeypatch.setattr(
        ss, "generate_text",
        lambda *, prompt, provider, model, options=None, think=None, **kw: captured.update(model=model, provider=provider, **kw)
        or {"text": "résumé", "provider": "ai_gateway", "model": model, "usage": None, "tokens": 1},
    )
    doc = SimpleNamespace(rag_space=SimpleNamespace(llm_provider="ollama", llm_model="llama3.1:8b"))
    chunks = [SimpleNamespace(text="contenu", page_number=None)]
    ss._perform_generation(doc, mode="bullets", scope="doc", page=None, language="fr", chunks=chunks)
    assert captured["model"] == "qwen3:30b-instruct"
    assert captured["provider"] == "ollama"


# --------------------------------------------------------------- câblage chat

def test_chat_uses_routed_factual_model(monkeypatch, settings):
    from apps.chat.services import chat_service as cs

    settings.RAG_TASK_ROUTING_ENABLED = True
    settings.RAG_TASK_MODEL_FACTUAL = "mistral-small3.2:24b"
    monkeypatch.setattr(cs, "normalize_llm_overrides", lambda space: ("ollama", "llama3.1:8b"))
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(cs, "mode_b_enabled", lambda: False)
    monkeypatch.setattr(cs, "_conversation_context_payload", lambda **k: "")
    monkeypatch.setattr(cs, "maybe_update_log", lambda *a, **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: {"status": "ok", "prompt": "PROMPT", "sources": [], "log_id": None, "answer_kind": "ok"},
    )

    captured = {}
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **kw: captured.update(kw) or {"text": "ok", "provider": "ai_gateway", "model": kw.get("model"), "usage": None, "tokens": 1},
    )
    rag_space = SimpleNamespace(llm_provider="ollama", llm_model="llama3.1:8b", allow_external_fallback=False)
    cs.chat_query(rag_space_id="rs", question="Quelle durée ?", rag_space=rag_space)
    assert captured["model"] == "mistral-small3.2:24b"
    assert captured["provider"] == "ollama"
    # Lot 1 : la décision de repli passe par le contexte d'egress résolu pour
    # l'espace. Espace ollama sans opt-in => aucun egress externe autorisé.
    assert captured["context"].external_egress_allowed is False


def test_chat_default_off_uses_workspace_model(monkeypatch, settings):
    from apps.chat.services import chat_service as cs

    settings.RAG_TASK_ROUTING_ENABLED = False
    settings.RAG_TASK_MODEL_FACTUAL = "mistral-small3.2:24b"
    monkeypatch.setattr(cs, "normalize_llm_overrides", lambda space: ("ollama", "llama3.1:8b"))
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(cs, "mode_b_enabled", lambda: False)
    monkeypatch.setattr(cs, "_conversation_context_payload", lambda **k: "")
    monkeypatch.setattr(cs, "maybe_update_log", lambda *a, **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: {"status": "ok", "prompt": "P", "sources": [], "log_id": None, "answer_kind": "ok"},
    )
    captured = {}
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **kw: captured.update(kw) or {"text": "ok", "provider": "ai_gateway", "model": kw.get("model"), "usage": None, "tokens": 1},
    )
    rag_space = SimpleNamespace(llm_provider="ollama", llm_model="llama3.1:8b", allow_external_fallback=False)
    cs.chat_query(rag_space_id="rs", question="q", rag_space=rag_space)
    assert captured["model"] == "llama3.1:8b"   # routage off => modèle workspace
