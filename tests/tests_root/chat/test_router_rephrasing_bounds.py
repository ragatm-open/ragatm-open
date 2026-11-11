"""Bornes du router pour la génération de suggestions : ``retry_on_empty`` et
propagation du ``timeout_s`` dédié jusqu'au chemin Gateway.

Ne touche PAS le comportement par défaut des autres appels ``generate_text`` :
le retry vide reste actif par défaut (retry_on_empty=True). Gateway entièrement
moqué : aucun appel réseau, aucun le serveur IA.
"""
from __future__ import annotations

import pytest

from apps.llm_router import router
from apps.llm_router.router import LLMError


@pytest.fixture(autouse=True)
def _gateway_on(monkeypatch):
    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)


def test_retry_on_empty_false_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def fake_via(**kwargs):
        calls["n"] += 1
        return {"text": "", "provider": "ollama", "model": "m"}

    monkeypatch.setattr(router, "_generate_via_gateway", fake_via)

    out = router.generate_text(prompt="p", provider="ollama", model="m", retry_on_empty=False)

    assert calls["n"] == 1  # aucune seconde tentative
    assert out["text"] == ""  # texte vide remonté tel quel (fallback géré par l'appelant)


def test_default_still_retries_once_on_empty(monkeypatch):
    calls = {"n": 0}

    def fake_via(**kwargs):
        calls["n"] += 1
        return {"text": "", "provider": "ollama", "model": "m"}

    monkeypatch.setattr(router, "_generate_via_gateway", fake_via)

    # Comportement par défaut INCHANGÉ : un retry, puis LLMError si toujours vide.
    with pytest.raises(LLMError):
        router.generate_text(prompt="p", provider="ollama", model="m")

    assert calls["n"] == 2


def test_timeout_s_is_forwarded_to_gateway(monkeypatch):
    cap: dict = {}

    def fake_via(**kwargs):
        cap.update(kwargs)
        return {"text": "ok", "provider": "ollama", "model": "m"}

    monkeypatch.setattr(router, "_generate_via_gateway", fake_via)

    router.generate_text(
        prompt="p", provider="ollama", model="m", timeout_s=3, retry_on_empty=False
    )
    assert cap["timeout_s"] == 3


def test_gateway_client_uses_forwarded_timeout(monkeypatch):
    """Le ``timeout_s`` atteint réellement le constructeur du client Gateway."""
    from apps.llm_router import router as r

    captured: dict = {}

    class FakeResult:
        def to_generate_text_dict(self):
            return {"text": "ok", "provider": "ollama", "model": "m"}

    class FakeClient:
        def __init__(self, *, base_url, timeout_s, enabled):
            captured["timeout_s"] = timeout_s

        def generate(self, req):
            return FakeResult()

    import apps.llm_router.gateway as gw

    monkeypatch.setattr(gw, "AIGatewayClient", FakeClient)
    # runtime_config par défaut (Gateway base_url vide OK : client moqué).
    r._generate_via_gateway(prompt="p", provider="ollama", model="m", timeout_s=2.5)
    assert captured["timeout_s"] == 2.5
