"""Tests PR 5c — routage de la génération via l'AI Gateway (derrière flag).

Couvre :
  1. flag False => chemin direct inchangé.
  2. flag True => AIGatewayClient.generate appelé.
  3. generate_text retourne exactement le dict historique.
  4. Gateway timeout + fallback enabled => erreur contrôlée (aucun repli en édition Open).
  5. Gateway timeout + fallback disabled => erreur typée.
  6. Aucun prompt dans les logs d'erreur.
  7. generate_text_stream reste legacy (jamais le Gateway).

HTTP réseau jamais touché : on mocke aux frontières (gateway client / providers).
"""
import pytest

import apps.llm_router.router as router
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.gateway import (
    GatewayGenerationResult,
    GatewayTimeoutError,
)


def _fake_ollama_result(model="m"):
    return router.LLMResult(
        text="Paris.", provider="ollama", model=model,
        usage={"prompt_eval_count": 2, "eval_count": 3}, raw={"ok": True},
    )


# --- Test 1 + 3 : flag False => chemin direct, contrat historique ----------

def test_flag_false_uses_direct_path(monkeypatch, settings):
    settings.AI_GATEWAY_ENABLED = False
    monkeypatch.setattr(
        router, "_ollama_generate",
        lambda *, prompt, model, timeout_s: _fake_ollama_result(model),
    )

    def _boom(**k):  # pragma: no cover
        raise AssertionError("gateway must not be used when disabled")

    monkeypatch.setattr(router, "_generate_via_gateway", _boom)

    out = router.generate_text(prompt="capitale ?", provider="ollama", model="m")
    assert set(out.keys()) == {"text", "tokens", "cost", "provider", "model", "usage", "raw"}
    assert out["text"] == "Paris."
    assert out["provider"] == "ollama"
    assert out["cost"] is None
    assert out["tokens"] == 5  # prompt_eval_count + eval_count


# --- Test 2 : flag True => AIGatewayClient.generate appelé -----------------

def test_flag_true_routes_via_gateway(monkeypatch, settings):
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw:8100"

    captured = {}

    def _fake_generate(self, req):
        captured["prompt"] = req.prompt
        captured["model"] = req.model
        captured["provider"] = req.provider
        return GatewayGenerationResult(
            text="Paris.", provider="ai_gateway", model=req.model, tokens=7,
            usage={"total_tokens": 7}, raw={"response": "Paris."},
        )

    monkeypatch.setattr(
        "apps.llm_router.gateway.client.AIGatewayClient.generate", _fake_generate
    )
    # Le chemin direct ne doit PAS être emprunté.
    monkeypatch.setattr(
        router, "_generate_text_direct",
        lambda **k: (_ for _ in ()).throw(AssertionError("direct must not run")),
    )

    out = router.generate_text(prompt="capitale ?", provider="ollama", model="m")
    assert out["provider"] == "ai_gateway"
    assert out["text"] == "Paris."
    assert out["tokens"] == 7
    assert set(out.keys()) == {"text", "tokens", "cost", "provider", "model", "usage", "raw"}
    # La requête Gateway a bien reçu prompt/model/provider résolus.
    assert captured["prompt"] == "capitale ?"
    assert captured["model"] == "m"
    assert captured["provider"] == "ollama"


# --- Test 2bis : provider/model Gateway DÉCOUPLÉS de RAG_LLM_PROVIDER ------

def test_gateway_default_uses_dedicated_provider_model(monkeypatch, settings):
    # generate_text(prompt=...) SANS provider/model explicites : la Gateway doit
    # recevoir AI_GATEWAY_PROVIDER/MODEL (ollama / llama3.1:8b), PAS RAG_LLM_PROVIDER.
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://ai-gateway:8100"
    settings.RAG_LLM_PROVIDER = "stub"        # ne doit PAS fuiter vers la Gateway
    settings.AI_GATEWAY_PROVIDER = "ollama"
    settings.AI_GATEWAY_MODEL = "llama3.1:8b"

    captured = {}

    def _fake_generate(self, req):
        captured["provider"] = req.provider
        captured["model"] = req.model
        return GatewayGenerationResult(
            text="OK", provider="ai_gateway", model=req.model, tokens=5,
            usage={"total_tokens": 5}, raw={},
        )

    monkeypatch.setattr(
        "apps.llm_router.gateway.client.AIGatewayClient.generate", _fake_generate
    )
    # Aucun chemin direct ne doit être appelé (Gateway OK, aucun repli).
    monkeypatch.setattr(
        router, "_generate_text_direct",
        lambda **k: (_ for _ in ()).throw(AssertionError("direct path must not be called")),
    )

    out = router.generate_text(prompt="q")
    assert captured["provider"] == "ollama"
    assert captured["model"] == "llama3.1:8b"
    assert out["provider"] == "ai_gateway"
    assert out["model"] == "llama3.1:8b"


def test_gateway_explicit_override_still_wins(monkeypatch, settings):
    # Un override explicite de l'appelant reste prioritaire sur AI_GATEWAY_PROVIDER/MODEL.
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_PROVIDER = "ollama"
    settings.AI_GATEWAY_MODEL = "llama3.1:8b"

    captured = {}

    def _fake_generate(self, req):
        captured["provider"] = req.provider
        captured["model"] = req.model
        return GatewayGenerationResult(
            text="OK", provider="ai_gateway", model=req.model, tokens=1, usage=None, raw={},
        )

    monkeypatch.setattr(
        "apps.llm_router.gateway.client.AIGatewayClient.generate", _fake_generate
    )
    router.generate_text(prompt="q", provider="ollama", model="mistral:7b")
    assert captured["provider"] == "ollama"
    assert captured["model"] == "mistral:7b"


# --- Test 4 : gateway échoue + fallback ON => erreur contrôlée (aucun repli n'existe) -

def test_gateway_failure_with_fallback_enabled_raises_controlled_error(monkeypatch, settings):
    """Édition Open : `AI_GATEWAY_FALLBACK_ENABLED=1` n'ouvre aucun provider — le seul
    repli admis est `none`, et il vaut une erreur typée, jamais Ollama direct."""
    from apps.llm_router.gateway import GatewayUnavailableError

    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "none"

    monkeypatch.setattr(
        router, "_generate_via_gateway",
        lambda **k: (_ for _ in ()).throw(GatewayTimeoutError("timeout")),
    )

    def _boom(**k):  # pragma: no cover
        raise AssertionError("no direct provider may serve a fallback")

    monkeypatch.setattr(router, "_generate_text_direct", _boom)

    with pytest.raises(GatewayUnavailableError):
        router.generate_text(prompt="x", provider="ollama", model="m")


# --- Test 5 : gateway échoue + fallback OFF => erreur typée ----------------

def test_gateway_failure_strict_raises(monkeypatch, settings):
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_ENABLED = False

    monkeypatch.setattr(
        router, "_generate_via_gateway",
        lambda **k: (_ for _ in ()).throw(GatewayTimeoutError("timeout")),
    )

    def _boom(**k):  # pragma: no cover
        raise AssertionError("no fallback when strict")

    monkeypatch.setattr(router, "_generate_text_direct", _boom)

    with pytest.raises(GatewayTimeoutError):
        router.generate_text(prompt="x", provider="ollama", model="m")


# --- Test 6 : aucun prompt dans les logs d'erreur --------------------------

def test_no_prompt_in_logs_on_fallback(monkeypatch, settings, caplog):
    from apps.llm_router.gateway import GatewayUnavailableError

    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "none"
    secret = "SECRET-CANARY-PROMPT-99"

    monkeypatch.setattr(
        router, "_generate_via_gateway",
        lambda **k: (_ for _ in ()).throw(GatewayTimeoutError("timeout")),
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(GatewayUnavailableError):
            router.generate_text(prompt=secret, provider="ollama", model="m")

    assert secret not in caplog.text


# --- Test 7 : streaming ne contourne JAMAIS la Gateway ---------------------

def test_stream_raises_when_gateway_enabled(monkeypatch, settings):
    # Policy : Gateway active => le stream est refusé (erreur contrôlée), il ne
    # retombe JAMAIS sur un provider direct.
    settings.AI_GATEWAY_ENABLED = True

    def _boom(**k):  # pragma: no cover
        raise AssertionError("stream must not call a direct provider when gateway ON")

    monkeypatch.setattr(router, "_ollama_stream", _boom)

    with pytest.raises(router.LLMError):
        list(router.generate_text_stream(prompt="q", provider="ollama", model="m"))


def test_stream_direct_when_gateway_disabled(monkeypatch, settings):
    # Gateway OFF (mode dégradé / tests) : le stream direct reste possible.
    settings.AI_GATEWAY_ENABLED = False

    def _fake_stream(*, prompt, model, timeout_s):
        yield {"type": "delta", "delta": "Pa"}
        yield {"type": "delta", "delta": "ris."}
        yield {"type": "result", "provider": "ollama", "model": model, "usage": None, "raw": None}

    monkeypatch.setattr(router, "_ollama_stream", _fake_stream)

    events = list(router.generate_text_stream(prompt="q", provider="ollama", model="m"))
    deltas = "".join(e["delta"] for e in events if e.get("type") == "delta")
    assert deltas == "Paris."
    assert events[-1]["type"] == "result"
