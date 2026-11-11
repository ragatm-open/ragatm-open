"""Invariants de configuration : ce qui doit être refusé, et quand.

Deux niveaux de défense, testés séparément :

1. **Au chargement des settings** — une valeur ambiguë ou hors bornes empêche le
   processus de démarrer. C'est le seul niveau qui protège aussi gunicorn, qui
   n'exécute PAS les system checks.
2. **Aux system checks** (`manage.py check`) — diagnostics à sévérité variable
   selon l'environnement.
"""
from __future__ import annotations

import pytest
from django.core.checks import Error
from django.core.checks import Warning as CheckWarning
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from config import checks as config_checks
from config import registry
from config.parsers import ConfigValueError
from config.registry.definition import SCOPES
from config.settings import from_registry

from .conftest import load_settings_with_environment, try_load_settings_with_environment


# ==========================================================================
# 1. Refus au chargement des settings
# ==========================================================================
@pytest.mark.parametrize(
    ("environment", "variable"),
    [
        ({"RAG_EMBEDDING_DIM": "1536"}, "RAG_EMBEDDING_DIM"),
        ({"AI_GATEWAY_TIMEOUT_SECONDS": "0"}, "AI_GATEWAY_TIMEOUT_SECONDS"),
        ({"AI_GATEWAY_TIMEOUT_SECONDS": "-5"}, "AI_GATEWAY_TIMEOUT_SECONDS"),
    ],
    ids=["embedding-dim-other-than-768", "gateway-timeout-zero", "gateway-timeout-negative"],
)
def test_out_of_domain_value_is_refused_at_load(environment, variable):
    ok, error = try_load_settings_with_environment(environment)
    assert not ok
    assert variable in error


@pytest.mark.parametrize(
    ("environment", "key", "expected"),
    [
        ({"RAG_EMBEDDING_DIM": "768"}, "RAG_EMBEDDING_DIM", 768),
        ({"AI_GATEWAY_URL": "http://ai-gateway:8100"}, "AI_GATEWAY_BASE_URL", "http://ai-gateway:8100"),
        ({"AI_GATEWAY_BASE_URL": "http://ai-gateway:8100"}, "AI_GATEWAY_BASE_URL", "http://ai-gateway:8100"),
    ],
    ids=[
        "embedding-dim-768-is-accepted",
        "deprecated-alias-alone-is-converted",
        "canonical-name-wins-when-alias-is-absent",
    ],
)
def test_in_domain_value_is_loaded(environment, key, expected):
    loaded = load_settings_with_environment(environment)
    assert loaded[key] == expected


def test_gateway_timeout_non_numeric_is_refused():
    ok, error = try_load_settings_with_environment({"AI_GATEWAY_TIMEOUT_SECONDS": "trente"})
    assert not ok


def test_ambiguous_legacy_boolean_is_refused_never_silently_activated():
    """`AI_GATEWAY_ENABLED=true` valait False hier. On refuse plutôt que d'activer."""
    ok, error = try_load_settings_with_environment({"AI_GATEWAY_ENABLED": "true"})
    assert not ok
    assert "ambiguë" in error or "ambigu" in error


@pytest.mark.parametrize("raw", ["0", "1"])
def test_legacy_boolean_accepts_the_canonical_literals(raw):
    loaded = load_settings_with_environment({"AI_GATEWAY_ENABLED": raw})
    assert loaded["AI_GATEWAY_ENABLED"] is (raw == "1")


def test_deprecated_alias_with_identical_value_is_accepted():
    loaded = load_settings_with_environment(
        {
            "AI_GATEWAY_URL": "http://ai-gateway:8100",
            "AI_GATEWAY_BASE_URL": "http://ai-gateway:8100",
        }
    )
    assert loaded["AI_GATEWAY_BASE_URL"] == "http://ai-gateway:8100"


def test_deprecated_alias_conflicting_with_canonical_is_refused():
    ok, error = try_load_settings_with_environment(
        {
            "AI_GATEWAY_URL": "http://ancienne-gateway:8100",
            "AI_GATEWAY_BASE_URL": "http://ai-gateway:8100",
        }
    )
    assert not ok
    assert "AI_GATEWAY_URL" in error


# ==========================================================================
# 2. Politique d'alias, hors chargement
# ==========================================================================
def test_resolve_aliases_reports_warning_for_lonely_old_name():
    canonical, warnings, errors = registry.resolve_aliases(
        {"DEFAULT_LLM_PROVIDER": "ollama"}
    )
    assert canonical == {"RAG_LLM_PROVIDER": "ollama"}
    assert warnings and not errors


def test_resolve_aliases_reports_error_on_conflict():
    _, _, errors = registry.resolve_aliases(
        {"DEFAULT_LLM_PROVIDER": "ollama", "RAG_LLM_PROVIDER": "stub"}
    )
    assert errors


def test_resolve_aliases_is_silent_without_deprecated_names():
    canonical, warnings, errors = registry.resolve_aliases({"RAG_LLM_PROVIDER": "ollama"})
    assert (canonical, warnings, errors) == ({}, [], [])


# ==========================================================================
# 3. Portées et valeurs d'override
# ==========================================================================
def test_override_scope_above_maximum_is_refused():
    definition = registry.require("AI_GATEWAY_TIMEOUT_SECONDS")
    definition.validate_scope("environment")
    with pytest.raises(ConfigValueError):
        definition.validate_scope("workspace")
    with pytest.raises(ConfigValueError):
        definition.validate_scope("ragspace")


def test_gateway_activation_is_no_longer_overridable():
    """Durcissement P0 : la politique IA ne se pilote plus par overlay."""
    definition = registry.require("AI_GATEWAY_ENABLED")
    for scope in SCOPES:
        with pytest.raises(ConfigValueError):
            definition.validate_scope(scope)


def test_override_value_out_of_bounds_is_refused():
    from apps.rag_core.core.config_resolver import coerce_value_for_storage

    assert coerce_value_for_storage("AI_GATEWAY_TIMEOUT_SECONDS", "30") == "30"
    with pytest.raises(ValueError):
        coerce_value_for_storage("AI_GATEWAY_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        coerce_value_for_storage("AI_GATEWAY_TIMEOUT_SECONDS", "1000")


def test_override_value_is_persisted_in_canonical_form():
    from apps.rag_core.core.config_resolver import coerce_value_for_storage

    for accepted in ("yes", "on", "TRUE", "1"):
        assert coerce_value_for_storage("AI_GATEWAY_ENABLED", accepted) == "true"
    for accepted in ("no", "off", "FALSE", "0"):
        assert coerce_value_for_storage("AI_GATEWAY_ENABLED", accepted) == "false"


def test_override_on_unknown_key_is_refused():
    from apps.rag_core.core.config_resolver import coerce_value_for_storage

    with pytest.raises(ValueError):
        coerce_value_for_storage("VARIABLE_INEXISTANTE", "1")


# ==========================================================================
# 4. System checks
# ==========================================================================
@override_settings(AI_GATEWAY_ENABLED=True, AI_GATEWAY_FALLBACK_ENABLED=False)
def test_gateway_without_fallback_is_no_longer_reported():
    """RAGatm Open : aucun fournisseur de repli n'existe, `config.W003` est sans objet."""
    issues = config_checks.check_gateway_without_fallback(None)
    assert issues == []


@override_settings(AI_GATEWAY_ENABLED=True, AI_GATEWAY_FALLBACK_ENABLED=True)
def test_gateway_with_fallback_is_silent():
    assert config_checks.check_gateway_without_fallback(None) == []


@override_settings(AI_GATEWAY_ENABLED=False, AI_GATEWAY_FALLBACK_ENABLED=False)
def test_fallback_disabled_without_gateway_is_silent():
    assert config_checks.check_gateway_without_fallback(None) == []


@override_settings(DJANGO_ENV="prod", DEBUG=True, ALLOWED_HOSTS=["exemple.test"])
def test_debug_in_production_is_an_error():
    ids = {i.id for i in config_checks.check_production_hardening(None)}
    assert "config.E003" in ids


@override_settings(DJANGO_ENV="prod", DEBUG=False, ALLOWED_HOSTS=[])
def test_empty_allowed_hosts_in_production_is_an_error():
    issues = config_checks.check_production_hardening(None)
    assert any("ALLOWED_HOSTS" in i.msg for i in issues)


@override_settings(DJANGO_ENV="prod", DEBUG=False, ALLOWED_HOSTS=["*"])
def test_wildcard_allowed_hosts_in_production_is_an_error():
    issues = config_checks.check_production_hardening(None)
    assert any("joker" in i.msg for i in issues)


@override_settings(
    DJANGO_ENV="prod",
    DEBUG=False,
    ALLOWED_HOSTS=["exemple.test"],
    DJANGO_SECRET_KEY="dev-secret-key",
)
def test_development_secret_key_in_production_is_an_error():
    issues = config_checks.check_production_hardening(None)
    assert any(i.msg.startswith("DJANGO_SECRET_KEY") for i in issues)
    # La valeur elle-même n'est JAMAIS reproduite dans le message.
    assert all("dev-secret-key" not in i.msg for i in issues)


@override_settings(
    DJANGO_ENV="prod",
    DEBUG=False,
    ALLOWED_HOSTS=["exemple.test"],
    ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=True,
)
def test_test_escape_hatch_in_production_is_an_error():
    issues = config_checks.check_production_hardening(None)
    assert any("ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS" in i.msg for i in issues)


@override_settings(
    DJANGO_ENV="prod",
    DEBUG=False,
    ALLOWED_HOSTS=["exemple.test"],
    INVITE_RETURN_RESET_LINK=True,
)
def test_reset_link_exposure_in_production_is_an_error():
    issues = config_checks.check_production_hardening(None)
    assert any("INVITE_RETURN_RESET_LINK" in i.msg for i in issues)


@override_settings(DJANGO_ENV="dev", DEBUG=True)
def test_production_hardening_is_silent_in_development():
    assert config_checks.check_production_hardening(None) == []


@override_settings(DJANGO_ENV="prod", DEBUG=False, ALLOWED_HOSTS=["exemple.test"],
                   CORE_CONFIG_EDIT_ENABLED=True)
def test_core_config_edit_in_production_is_an_error():
    issues = config_checks.check_production_hardening(None)
    assert any(
        isinstance(i, Error) and "CORE_CONFIG_EDIT_ENABLED" in i.msg for i in issues
    )


@override_settings(DJANGO_ENV="staging", DEBUG=False, ALLOWED_HOSTS=["exemple.test"],
                   CORE_CONFIG_EDIT_ENABLED=True)
def test_core_config_edit_in_staging_is_only_a_warning():
    issues = config_checks.check_production_hardening(None)
    editing = [i for i in issues if "CORE_CONFIG_EDIT_ENABLED" in i.msg]
    assert editing and all(isinstance(i, CheckWarning) for i in editing)


def test_runtime_overlay_without_shared_cache_is_refused():
    """Invariant historique, revérifié ici : il conditionne tout le lot Core."""
    from apps.rag_core.apps import check_runtime_config_cache

    with override_settings(CORE_CONFIG_RUNTIME_ENABLED=True, DJANGO_CACHE_URL=""):
        assert [i.id for i in check_runtime_config_cache(None)] == ["rag_core.E003"]

    with override_settings(
        CORE_CONFIG_RUNTIME_ENABLED=True, DJANGO_CACHE_URL="redis://redis:6379/2"
    ):
        assert check_runtime_config_cache(None) == []

    with override_settings(CORE_CONFIG_RUNTIME_ENABLED=False, DJANGO_CACHE_URL=""):
        assert check_runtime_config_cache(None) == []


def test_gateway_enabled_without_url_is_refused():
    """Invariant historique : Gateway active sans URL => erreur bloquante."""
    from apps.llm_router.checks import check_ai_gateway_base_url

    with override_settings(
        DJANGO_ENV="staging", AI_GATEWAY_ENABLED=True, AI_GATEWAY_BASE_URL=""
    ):
        assert [i.id for i in check_ai_gateway_base_url(None)] == ["llm_router.E002"]

    with override_settings(
        DJANGO_ENV="staging",
        AI_GATEWAY_ENABLED=True,
        AI_GATEWAY_BASE_URL="http://ai-gateway:8100",
    ):
        assert check_ai_gateway_base_url(None) == []


# ==========================================================================
# 5. Conformité des settings au registre
# ==========================================================================
def test_registry_conformance_is_clean_on_the_current_settings():
    assert config_checks.check_registry_conformance(None) == []


@override_settings(AI_GATEWAY_TIMEOUT_SECONDS=100000)
def test_registry_conformance_warns_on_out_of_bounds_setting():
    issues = config_checks.check_registry_conformance(None)
    assert any(i.id == "config.W002" for i in issues)


@override_settings(RAG_TOP_K="pas-un-entier")
def test_registry_conformance_errors_on_wrong_type():
    issues = config_checks.check_registry_conformance(None)
    assert any(i.id == "config.E002" for i in issues)


def test_deferred_anomalies_are_reported_by_a_check():
    from_registry.DEFERRED_ANOMALIES.append(("UNE_URL", "URL sans schéma"))
    try:
        issues = config_checks.check_settings_load_anomalies(None)
        assert any("UNE_URL" in i.msg for i in issues)
    finally:
        from_registry.DEFERRED_ANOMALIES.clear()
