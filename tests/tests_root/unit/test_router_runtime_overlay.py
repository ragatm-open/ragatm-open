"""Tests PR-G3 — router/AI Gateway lit ses réglages via runtime_config.

Parité stricte flag off ; overlay DB pris en compte uniquement flag on (scope
environment/global, le router n'a pas de contexte workspace).

Depuis le durcissement P0, le câblage est prouvé sur une clé qui reste
**éditable** (`AI_GATEWAY_TIMEOUT_SECONDS`). `AI_GATEWAY_ENABLED` et
`AI_GATEWAY_BASE_URL` sont sorties du périmètre éditable — un override ne peut
plus basculer la politique IA ni rerouter les embeddings. Les cas
correspondants vérifient donc désormais que l'override est **ignoré**
(cf. `test_overlay_runtime_hardening.py`).
"""
import pytest
from django.core.cache import cache

import apps.llm_router.router as router

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _ovr(scope_type, scope_ref, key, value):
    from apps.rag_core.models import CoreConfigOverride

    CoreConfigOverride.objects.create(scope_type=scope_type, scope_ref=scope_ref, key=key, value=value)


# --- Parité flag off ---------------------------------------------------------

def test_flag_off_gateway_enabled_reads_settings(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.AI_GATEWAY_ENABLED = True
    assert router._gateway_enabled() is True
    settings.AI_GATEWAY_ENABLED = False
    assert router._gateway_enabled() is False


def test_flag_off_override_ignored(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.AI_GATEWAY_ENABLED = False
    _ovr("global", "", "AI_GATEWAY_ENABLED", "true")
    assert router._gateway_enabled() is False


def test_flag_off_fallback_reads_settings(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    assert router._gateway_fallback_enabled() is True


# --- Flag on : overlay pilote -----------------------------------------------

def test_flag_on_override_cannot_flip_gateway_activation(settings):
    """`AI_GATEWAY_ENABLED` n'est plus pilotable : l'override est ignoré."""
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_ENABLED = False
    _ovr("global", "", "AI_GATEWAY_ENABLED", "true")
    assert router._gateway_enabled() is False


def test_flag_on_environment_override_cannot_flip_gateway_activation(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.DJANGO_ENV = "dev"
    settings.AI_GATEWAY_ENABLED = False
    _ovr("environment", "dev", "AI_GATEWAY_ENABLED", "true")
    assert router._gateway_enabled() is False


def test_flag_on_timeout_override_is_applied(settings):
    """Le câblage PR-G3 reste prouvé, sur une clé éditable."""
    from apps.rag_core.core import runtime_config

    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_TIMEOUT_SECONDS = 30
    _ovr("global", "", "AI_GATEWAY_TIMEOUT_SECONDS", "45")
    assert runtime_config.get("AI_GATEWAY_TIMEOUT_SECONDS") == 45


def test_flag_on_fallback_override(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    _ovr("global", "", "AI_GATEWAY_FALLBACK_ENABLED", "false")
    assert router._gateway_fallback_enabled() is False


def test_flag_on_no_override_falls_back_to_settings(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_ENABLED = True
    assert router._gateway_enabled() is True


# --- _generate_via_gateway construit le client via runtime_config ----------

def test_generate_via_gateway_uses_runtime_values(settings, monkeypatch):
    """Le client est construit depuis `runtime_config`.

    `AI_GATEWAY_BASE_URL` n'étant plus éditable, l'URL vient des settings et
    l'override posé en base doit être IGNORÉ ; seul le timeout est piloté.
    """
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw-settings:8100"
    _ovr("global", "", "AI_GATEWAY_BASE_URL", "http://gw-overlay:8100")
    _ovr("global", "", "AI_GATEWAY_TIMEOUT_SECONDS", "12")

    captured = {}

    def _fake_generate(self, req):
        from apps.llm_router.gateway import GatewayGenerationResult

        captured["base_url"] = self.base_url
        captured["timeout"] = self.timeout_s
        captured["enabled"] = self.enabled
        return GatewayGenerationResult(text="ok", provider="ai_gateway", model="m", tokens=0, usage=None, raw={})

    monkeypatch.setattr("apps.llm_router.gateway.client.AIGatewayClient.generate", _fake_generate)

    out = router.generate_text(prompt="x", provider="ollama", model="m")
    assert out["provider"] == "ai_gateway"
    # Le timeout vient de l'overlay ; l'URL reste celle des settings.
    assert captured["timeout"] == 12
    assert captured["base_url"] == "http://gw-settings:8100"
    assert captured["enabled"] is True
