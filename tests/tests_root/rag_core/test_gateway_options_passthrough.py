"""Tests unitaires : sélection explicite du modèle + passthrough options/think, et
garde locale (prompt blanc). Aucun réseau (client Gateway mocké).
"""
from __future__ import annotations

import pytest

from apps.llm_router.gateway.client import AIGatewayClient
from apps.llm_router.gateway.dto import GatewayGenerationRequest
from apps.llm_router.gateway.exceptions import GatewayConfigError


# ------------------------------------------------------------ DTO to_payload

def test_payload_omits_optional_fields_by_default():
    """Parité stricte : options/think/system absents quand non fournis.

    `stream` et `provider` ont été retirés du FIL par le rétroportage canonique
    (hors contrat `GenerateRequest`) ; `provider` reste porté par le DTO pour le
    routage interne.
    """
    req = GatewayGenerationRequest(prompt="hi", provider="ollama", model="llama3.1:8b")
    p = req.to_payload()
    assert p == {"prompt": "hi", "model": "llama3.1:8b"}
    assert "options" not in p and "think" not in p and "system" not in p
    assert req.provider == "ollama", "le provider interne n'est pas effacé"


def test_payload_includes_options_think_system_when_set():
    p = GatewayGenerationRequest(
        prompt="hi", model="qwen3:14b",
        options={"num_ctx": 8192, "num_predict": 1024, "temperature": 0.1},
        think=False, system="tu es un assistant",
    ).to_payload()
    assert p["options"] == {"num_ctx": 8192, "num_predict": 1024, "temperature": 0.1}
    assert p["think"] is False
    assert p["system"] == "tu es un assistant"


def test_payload_think_true_is_emitted():
    p = GatewayGenerationRequest(prompt="x", think=True).to_payload()
    assert p["think"] is True


# ------------------------------------------------------------ local blank-prompt guard

def test_blank_prompt_rejected_locally_no_network(monkeypatch):
    import apps.llm_router.gateway.client as client_mod

    def _boom(*a, **k):  # tout appel réseau => échec du test
        raise AssertionError("network call must not happen for blank prompt")

    monkeypatch.setattr(client_mod.requests, "post", _boom)
    client = AIGatewayClient(base_url="http://gw:8100", timeout_s=5, enabled=True)
    with pytest.raises(GatewayConfigError):
        client.generate(GatewayGenerationRequest(prompt="   \n\t", model="llama3.1:8b"))


# ------------------------------------------------------------ router passthrough

def test_generate_via_gateway_passes_options_and_think(monkeypatch):
    from apps.llm_router import router

    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured["client_kwargs"] = kw

        def generate(self, req):
            captured["req"] = req

            class _R:
                def to_generate_text_dict(self_inner):
                    return {"text": "ok", "tokens": 1, "provider": "ai_gateway", "model": req.model}

            return _R()

    # runtime_config renvoie les valeurs par défaut d'un env gateway
    monkeypatch.setattr(router, "AIGatewayClient", FakeClient, raising=False)
    # patch l'import interne du module gateway
    import apps.llm_router.gateway as gw
    monkeypatch.setattr(gw, "AIGatewayClient", FakeClient, raising=True)

    out = router._generate_via_gateway(
        prompt="p", provider="ollama", model="qwen3:14b",
        options={"num_predict": 1024, "temperature": 0.1}, think=False,
    )
    req = captured["req"]
    assert req.model == "qwen3:14b"
    assert req.provider == "ollama"
    assert req.options == {"num_predict": 1024, "temperature": 0.1}
    assert req.think is False
    assert out["text"] == "ok"


def test_generate_text_threads_options_to_gateway(monkeypatch):
    from apps.llm_router import router

    seen = {}

    def fake_via_gateway(*, prompt, provider, model, options=None, think=None, request_id=None):
        seen.update(prompt=prompt, provider=provider, model=model, options=options, think=think)
        return {"text": "done", "tokens": 0, "provider": "ai_gateway", "model": model}

    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_generate_via_gateway", fake_via_gateway)

    router.generate_text(
        prompt="q", model="mistral-small3.2:24b",
        options={"num_ctx": 8192, "num_predict": 2048}, think=None,
    )
    assert seen["model"] == "mistral-small3.2:24b"
    assert seen["options"] == {"num_ctx": 8192, "num_predict": 2048}


def test_generate_text_default_options_none_preserves_parity(monkeypatch):
    """Sans options, generate_text transmet options=None (comportement historique)."""
    from apps.llm_router import router
    seen = {}

    def fake_via_gateway(*, prompt, provider, model, options=None, think=None, request_id=None):
        seen["options"] = options
        return {"text": "x", "tokens": 0, "provider": "ai_gateway", "model": model}

    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_generate_via_gateway", fake_via_gateway)
    router.generate_text(prompt="q", model="llama3.1:8b")
    assert seen["options"] is None
