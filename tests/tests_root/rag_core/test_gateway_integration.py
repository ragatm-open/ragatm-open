"""Tests d'INTÉGRATION contre l'AI Gateway réelle (opt-in réseau).

Ne s'exécutent PAS par défaut — ni leur corps, ni leur sonde de disponibilité.
Lancer explicitement :
    make test-backend-external
    # ou : RAG_RUN_EXTERNAL_TESTS=1 pytest -m "external and integration"

Ignorés proprement si la Gateway n'est pas joignable (pas d'échec dur).

La sonde `_gateway_reachable()` vivait dans un `pytest.mark.skipif` de niveau
module : elle partait donc à la COLLECTE, y compris pour un simple
`pytest --collect-only` de la suite standard. Elle est désormais appelée depuis
une fixture, c'est-à-dire seulement quand le test s'exécute vraiment.
"""
from __future__ import annotations

import pytest
import requests

from django.conf import settings

from testing.external import external, skip_unless_reachable

from apps.llm_router.gateway import GatewayError
from apps.llm_router.gateway.client import AIGatewayClient
from apps.llm_router.gateway.dto import GatewayGenerationRequest

pytestmark = external(pytest.mark.integration)

BASE_URL = getattr(settings, "AI_GATEWAY_BASE_URL", "") or "http://ai-gateway:8100"


def _gateway_reachable() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


@pytest.fixture(autouse=True)
def _gateway_up():
    """Sonde de disponibilité — à l'exécution, jamais à la collecte."""
    skip_unless_reachable(_gateway_reachable, "AI Gateway")


def test_explicit_model_selection_roundtrip():
    client = AIGatewayClient(base_url=BASE_URL, timeout_s=60, enabled=True)
    req = GatewayGenerationRequest(
        prompt="Réponds uniquement par le mot: OK",
        provider="ollama", model="llama3.1:8b",
        options={"num_ctx": 2048, "num_predict": 16, "temperature": 0.0},
    )
    out = client.generate(req).to_generate_text_dict()
    assert out["model"] == "llama3.1:8b"
    assert isinstance(out["text"], str) and out["text"].strip()
    assert out["tokens"] >= 1


def test_absent_model_raises_gateway_error():
    client = AIGatewayClient(base_url=BASE_URL, timeout_s=30, enabled=True)
    req = GatewayGenerationRequest(prompt="hi", provider="ollama", model="does-not-exist:999b")
    with pytest.raises(GatewayError):
        client.generate(req)


def test_embeddings_dimension_768():
    from apps.llm_router.embeddings import _gateway_embed

    vecs = _gateway_embed(
        ["contrat alpha"], model="nomic-embed-text", task="document",
        dim=768, base_url=BASE_URL, timeout_s=30,
    )
    assert len(vecs) == 1 and len(vecs[0]) == 768
