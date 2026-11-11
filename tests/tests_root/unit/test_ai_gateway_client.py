"""Tests unitaires — PR 5a : client AI Gateway interne (INERTE).

Couvre :
  1. flag False => aucun appel Gateway (aucune requête réseau).
  2. le client sérialise correctement une demande de génération.
  3. timeout mappé en erreur typée.
  4. réponse Gateway mappée vers le format `generate_text`.
  5. generate_text legacy inchangé (le router n'utilise pas le Gateway).

HTTP entièrement mocké : aucun appel réseau réel.
"""
import pytest
import requests

import apps.llm_router.gateway.client as gw_client
from apps.llm_router.gateway import (
    AIGatewayClient,
    GatewayDisabledError,
    GatewayGenerationRequest,
    GatewayGenerationResult,
    GatewayResponseError,
    GatewayTimeoutError,
)


class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


# --- Test 2 : sérialisation de la requête ---------------------------------

def test_request_serialization_omits_empty_fields():
    req = GatewayGenerationRequest(prompt="Bonjour")
    assert req.to_payload() == {"prompt": "Bonjour"}


def test_request_serialization_full():
    """Corps CANONIQUE : l'état interne reste riche, le fil reste minimal.

    `stream`, `provider` et `request_id` figuraient ici jusqu'au rétroportage
    canonique. Ils n'ont jamais appartenu au schéma `GenerateRequest` de la
    passerelle : la candidate stricte les refuse en 400 `extra_forbidden`. Ils
    restent portés par le DTO, jamais sérialisés.
    """
    req = GatewayGenerationRequest(
        prompt="Q",
        provider="ollama",
        model="llama3.1:8b",
        options={"temperature": 0.2},
        request_id="rid-123",
    )
    assert req.to_payload() == {
        "prompt": "Q",
        "model": "llama3.1:8b",
        "options": {"temperature": 0.2},
    }
    # L'état interne survit à la réparation du fil.
    assert req.provider == "ollama" and req.request_id == "rid-123" and req.stream is False


# --- Test 1 : flag False => aucun appel réseau ----------------------------

def test_disabled_client_never_calls_network(monkeypatch, settings):
    settings.AI_GATEWAY_ENABLED = False
    settings.AI_GATEWAY_BASE_URL = "http://ai-gateway:8100"

    def _boom(*a, **k):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("no network call expected when gateway disabled")

    monkeypatch.setattr(gw_client.requests, "post", _boom)

    client = AIGatewayClient.from_settings()
    assert client.enabled is False
    with pytest.raises(GatewayDisabledError):
        client.generate(GatewayGenerationRequest(prompt="x"))


def test_gateway_enabled_reads_setting(settings):
    settings.AI_GATEWAY_ENABLED = False
    assert gw_client.gateway_enabled() is False
    settings.AI_GATEWAY_ENABLED = True
    assert gw_client.gateway_enabled() is True


# --- Test 3 : timeout mappé en erreur typée -------------------------------

def test_timeout_is_mapped_to_typed_error(monkeypatch):
    def _raise_timeout(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(gw_client.requests, "post", _raise_timeout)

    client = AIGatewayClient(base_url="http://gw:8100", timeout_s=5, enabled=True)
    with pytest.raises(GatewayTimeoutError):
        client.generate(GatewayGenerationRequest(prompt="x"))


def test_connection_error_is_mapped(monkeypatch):
    def _raise_conn(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(gw_client.requests, "post", _raise_conn)
    client = AIGatewayClient(base_url="http://gw:8100", timeout_s=5, enabled=True)
    with pytest.raises(GatewayResponseError):
        client.generate(GatewayGenerationRequest(prompt="x"))


def test_http_error_is_mapped(monkeypatch):
    monkeypatch.setattr(
        gw_client.requests, "post",
        lambda *a, **k: _FakeResponse(status_code=503, text="unavailable"),
    )
    client = AIGatewayClient(base_url="http://gw:8100", timeout_s=5, enabled=True)
    with pytest.raises(GatewayResponseError):
        client.generate(GatewayGenerationRequest(prompt="x"))


# --- Test 4 : réponse mappée vers le format generate_text -----------------

def test_response_mapped_to_generate_text_dict(monkeypatch):
    payload = {
        "text": "Paris.",
        "provider": "ollama",
        "model": "llama3.1:8b",
        "tokens": 7,
        "usage": {"eval_count": 7},
        "extra": "ignored-but-kept-in-raw",
    }
    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["timeout"] = kwargs.get("timeout")
        return _FakeResponse(status_code=200, payload=payload)

    monkeypatch.setattr(gw_client.requests, "post", _fake_post)

    client = AIGatewayClient(base_url="http://gw:8100", timeout_s=12, enabled=True)
    result = client.generate_dict(GatewayGenerationRequest(prompt="capitale ?", provider="ollama"))

    # Contrat identique à router.generate_text
    assert set(result.keys()) == {"text", "tokens", "cost", "provider", "model", "usage", "raw"}
    assert result["text"] == "Paris."
    assert result["tokens"] == 7
    assert result["cost"] is None
    assert result["provider"] == "ollama"
    assert result["model"] == "llama3.1:8b"
    assert result["usage"] == {"eval_count": 7}
    assert result["raw"] == payload  # raw conserve la réponse complète
    # Le client a bien tapé le bon endpoint avec timeout explicite.
    assert captured["url"] == "http://gw:8100/v1/generate"
    assert captured["timeout"] == 12
    assert captured["json"]["prompt"] == "capitale ?"


def test_from_response_rejects_non_dict():
    with pytest.raises(ValueError):
        GatewayGenerationResult.from_response([1, 2, 3])  # type: ignore[arg-type]


def test_from_response_compatible_with_gateway_contract_pr5b():
    # Test 10 (PR 5b) : la réponse EXACTE du service AI Gateway (tokens imbriqués
    # + usage) est correctement mappée par le client RagATM, total de tokens inclus.
    gateway_response = {
        "text": "Paris.",
        "provider": "ai_gateway",
        "model": "llama3.1:8b",
        "tokens": {"prompt": 5, "completion": 2, "total": 7},
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        "cost": 0.0,
        "raw": {"response": "Paris."},
    }
    res = GatewayGenerationResult.from_response(gateway_response)
    assert res.text == "Paris."
    assert res.provider == "ai_gateway"
    assert res.model == "llama3.1:8b"
    assert res.tokens == 7  # dérivé de tokens.total / usage.total_tokens
    assert res.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

    d = res.to_generate_text_dict()
    assert set(d.keys()) == {"text", "tokens", "cost", "provider", "model", "usage", "raw"}
    assert d["tokens"] == 7
    assert d["provider"] == "ai_gateway"


def test_invalid_json_mapped_to_typed_error(monkeypatch):
    class _BadJSON(_FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(gw_client.requests, "post", lambda *a, **k: _BadJSON(status_code=200))
    client = AIGatewayClient(base_url="http://gw:8100", timeout_s=5, enabled=True)
    with pytest.raises(GatewayResponseError):
        client.generate(GatewayGenerationRequest(prompt="x"))


# --- Test 5 : generate_text legacy inchangé -------------------------------

def test_generate_text_legacy_unchanged(monkeypatch):
    """Le router ne passe PAS par le Gateway : provider direct, contrat inchangé."""
    import apps.llm_router.router as router

    monkeypatch.setattr(
        router,
        "_ollama_generate",
        lambda *, prompt, model, timeout_s: router.LLMResult(
            text="Paris.", provider="ollama", model=model, usage={"eval_count": 3}, raw={}
        ),
    )
    out = router.generate_text(prompt="capitale ?", provider="ollama", model="m")
    assert set(out.keys()) == {"text", "tokens", "cost", "provider", "model", "usage", "raw"}
    assert out["text"] == "Paris."
    assert out["provider"] == "ollama"
