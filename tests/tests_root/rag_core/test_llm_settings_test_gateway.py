"""Tests : « Tester la connexion » sonde l'AI Gateway (pas Ollama direct) sous Gateway.

Corrige le faux « Ollama injoignable » : quand AI_GATEWAY_ENABLED, le test doit
interroger la Gateway (/v1/models) et vérifier la présence du modèle. Mocks réseau.
"""
from __future__ import annotations

import pytest
import requests

from apps.llm_router import capabilities as cap


# ----------------------------------------------------------------- _model_served

def test_model_served_exact_and_family():
    names = ["mistral-small3.2:24b", "llama3.1:8b", "nomic-embed-text:latest"]
    assert cap._model_served("mistral-small3.2:24b", names)      # exact
    assert cap._model_served("nomic-embed-text", names)          # famille ↔ :latest
    assert cap._model_served("llama3.1:8b", names)
    assert cap._model_served("", names)                          # vide => toléré
    assert not cap._model_served("gpt-4o", names)                # absent


# ----------------------------------------------------------------- _test_gateway

class _Resp:
    def __init__(self, status_code=200, models=None):
        self.status_code = status_code
        self._models = models if models is not None else []

    def json(self):
        return {"provider": "ollama", "models": [{"name": n} for n in self._models]}


def _enable_gateway(settings):
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw:8100"


def test_gateway_ok_when_model_present(monkeypatch, settings):
    _enable_gateway(settings)
    monkeypatch.setattr(cap.requests, "get", lambda *a, **k: _Resp(200, ["mistral-small3.2:24b", "llama3.1:8b"]))
    out = cap.test_provider_connection("ollama", "mistral-small3.2:24b")
    assert out["ok"] is True and out.get("via") == "ai_gateway"


def test_gateway_model_not_available(monkeypatch, settings):
    _enable_gateway(settings)
    monkeypatch.setattr(cap.requests, "get", lambda *a, **k: _Resp(200, ["llama3.1:8b"]))
    out = cap.test_provider_connection("ollama", "mistral-small3.2:24b")
    assert out["ok"] is False and out["code"] == "model_not_available"


def test_gateway_unreachable(monkeypatch, settings):
    _enable_gateway(settings)

    def _boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(cap.requests, "get", _boom)
    out = cap.test_provider_connection("ollama", "mistral-small3.2:24b")
    assert out["ok"] is False and out["code"] == "gateway_unreachable"


def test_gateway_base_url_missing(monkeypatch, settings):
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = ""
    out = cap.test_provider_connection("ollama", "llama3.1:8b")
    assert out["ok"] is False and out["code"] == "gateway_base_url_missing"


def test_gateway_disabled_uses_direct_ollama(monkeypatch, settings):
    settings.AI_GATEWAY_ENABLED = False
    called = {"gw": 0}
    monkeypatch.setattr(cap, "_test_gateway", lambda m: called.__setitem__("gw", called["gw"] + 1) or {"ok": True})
    monkeypatch.setattr(cap, "_test_ollama", lambda: {"ok": True, "provider": "ollama", "direct": True})
    out = cap.test_provider_connection("ollama", "llama3.1:8b")
    assert out.get("direct") is True and called["gw"] == 0   # Gateway non utilisée
