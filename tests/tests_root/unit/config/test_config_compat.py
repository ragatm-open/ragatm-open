"""Compatibilité avant/après : la consolidation ne change AUCUNE valeur effective.

Le passage des settings au registre remplace des expressions écrites à la main
(`os.getenv(...) == "1"`, `int(os.getenv(...))`) par un parsing déclaratif. Ce
fichier rejoue les expressions HISTORIQUES et exige l'égalité stricte avec la
valeur produite aujourd'hui, sur des profils de configuration représentatifs des
déploiements réels.

Les cas où l'égalité est volontairement rompue (valeur ambiguë) sont testés
séparément dans `test_config_invariants.py` : le nouveau comportement y est un
REFUS de démarrer, jamais une interprétation différente.
"""
from __future__ import annotations

import pytest

from .conftest import load_settings_with_environment

# --------------------------------------------------------------------------
# Profils représentatifs (§10.4 du cahier des charges du lot)
# --------------------------------------------------------------------------
PROFILES: dict[str, dict[str, str]] = {
    "dev-defaut": {},
    "test": {
        "DJANGO_ENV": "test",
        "EMBEDDINGS_PROVIDER": "stub",
        "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS": "1",
        "CELERY_TASK_ALWAYS_EAGER": "1",
    },
    "staging-gateway-inactive": {
        "DJANGO_ENV": "test",  # évite la garde prod du module de settings
        "AI_GATEWAY_ENABLED": "0",
        "AI_GATEWAY_FALLBACK_ENABLED": "1",
        "EMBEDDINGS_PROVIDER": "ai_gateway",
    },
    "staging-gateway-active": {
        "DJANGO_ENV": "test",
        "AI_GATEWAY_ENABLED": "1",
        "AI_GATEWAY_BASE_URL": "http://ai-gateway:8100",
        "AI_GATEWAY_TIMEOUT_SECONDS": "45",
        "AI_GATEWAY_FALLBACK_ENABLED": "0",
        "AI_GATEWAY_FALLBACK_PROVIDER": "none",
    },
    "staging-overlay-diagnostic": {
        "DJANGO_ENV": "test",
        "CORE_CONFIG_EDIT_ENABLED": "1",
        "CORE_CONFIG_RUNTIME_ENABLED": "0",
    },
    "staging-overlay-runtime": {
        "DJANGO_ENV": "test",
        "CORE_CONFIG_EDIT_ENABLED": "1",
        "CORE_CONFIG_RUNTIME_ENABLED": "1",
        "DJANGO_CACHE_URL": "redis://redis:6379/2",
    },
    "prod-conservative": {
        "DJANGO_ENV": "test",
        "AI_GATEWAY_ENABLED": "0",
        "CORE_CONFIG_EDIT_ENABLED": "0",
        "CORE_CONFIG_RUNTIME_ENABLED": "0",
        "EMBEDDINGS_PROVIDER": "ai_gateway",
    },
    "prod-gateway-via-env": {
        "DJANGO_ENV": "test",
        "AI_GATEWAY_ENABLED": "1",
        "AI_GATEWAY_BASE_URL": "http://ai-gateway:8100",
        "AI_GATEWAY_MODEL": "mistral-small3.2:24b",
        "AI_GATEWAY_PROVIDER": "ollama",
        "AI_GATEWAY_FALLBACK_ENABLED": "1",
        "AI_GATEWAY_FALLBACK_PROVIDER": "none",
        "EMBEDDINGS_PROVIDER": "ai_gateway",
    },
    "gateway-via-alias-deprecie": {
        "DJANGO_ENV": "test",
        "AI_GATEWAY_ENABLED": "1",
        "AI_GATEWAY_URL": "http://ai-gateway:8100",
    },
}


# --------------------------------------------------------------------------
# Réimplémentation des expressions HISTORIQUES (référence de non-régression)
# --------------------------------------------------------------------------
def _legacy_expected(env: dict[str, str]) -> dict[str, object]:
    """Ce que `settings/base.py` produisait AVANT la consolidation."""
    def strict_one(name: str, default: str) -> bool:
        return env.get(name, default) == "1"

    base_url = env.get("AI_GATEWAY_BASE_URL") or env.get("AI_GATEWAY_URL") or ""

    return {
        "AI_GATEWAY_ENABLED": strict_one("AI_GATEWAY_ENABLED", "0"),
        "AI_GATEWAY_BASE_URL": base_url,
        "AI_GATEWAY_TIMEOUT_SECONDS": int(env.get("AI_GATEWAY_TIMEOUT_SECONDS", "30")),
        "AI_GATEWAY_PROVIDER": (env.get("AI_GATEWAY_PROVIDER", "ollama") or "ollama")
        .strip()
        .lower(),
        # Défaut de référence : nom LOGIQUE du registre Gateway Open (décision,
        # remplace la valeur provisoire `llama3.1:8b` héritée du programme privé).
        "AI_GATEWAY_MODEL": (env.get("AI_GATEWAY_MODEL", "compact-generation-8") or "compact-generation-8").strip(),
        "AI_GATEWAY_FALLBACK_ENABLED": strict_one("AI_GATEWAY_FALLBACK_ENABLED", "0"),
        "AI_GATEWAY_FALLBACK_PROVIDER": (
            env.get("AI_GATEWAY_FALLBACK_PROVIDER", "none") or "none"
        )
        .strip()
        .lower(),
        "AI_GATEWAY_REQUIRED_BASE_URL": env.get("AI_GATEWAY_REQUIRED_BASE_URL", ""),
        "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS": strict_one(
            "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS", "0"
        ),
        "CORE_CONFIG_EDIT_ENABLED": strict_one("CORE_CONFIG_EDIT_ENABLED", "0"),
        "CORE_CONFIG_RUNTIME_ENABLED": strict_one("CORE_CONFIG_RUNTIME_ENABLED", "0"),
        "DJANGO_CACHE_URL": env.get("DJANGO_CACHE_URL", "").strip(),
        "RAG_EMBEDDING_DIM": int(env.get("RAG_EMBEDDING_DIM", "768")),
    }


@pytest.mark.parametrize("profile", sorted(PROFILES), ids=lambda name: name)
def test_migrated_settings_keep_their_historical_value(profile):
    env = PROFILES[profile]
    loaded = load_settings_with_environment(env)
    expected = _legacy_expected(env)

    divergences = [
        f"{name}: avant={value!r} après={loaded[name]!r}"
        for name, value in expected.items()
        if loaded.get(name) != value
    ]
    assert not divergences, (
        f"Profil {profile} : la consolidation a changé une valeur effective :\n"
        + "\n".join(divergences)
    )


@pytest.mark.parametrize("profile", sorted(PROFILES), ids=lambda name: name)
def test_cache_backend_choice_is_unchanged(profile):
    """Redis dès que DJANGO_CACHE_URL est posée, LocMemCache sinon."""
    env = PROFILES[profile]
    loaded = load_settings_with_environment(env)
    expected_redis = bool(env.get("DJANGO_CACHE_URL", "").strip())
    caches = str(loaded.get("CACHES", ""))
    assert ("RedisCache" in caches) is expected_redis, profile


def test_runtime_overlay_profile_carries_a_shared_cache():
    """Le profil « overlay runtime actif » ne peut PAS tourner sans cache partagé."""
    loaded = load_settings_with_environment(PROFILES["staging-overlay-runtime"])
    assert loaded["CORE_CONFIG_RUNTIME_ENABLED"] is True
    assert loaded["DJANGO_CACHE_URL"].startswith("redis://")


def test_diagnostic_overlay_profile_needs_no_cache():
    loaded = load_settings_with_environment(PROFILES["staging-overlay-diagnostic"])
    assert loaded["CORE_CONFIG_RUNTIME_ENABLED"] is False
    assert loaded["DJANGO_CACHE_URL"] == ""
