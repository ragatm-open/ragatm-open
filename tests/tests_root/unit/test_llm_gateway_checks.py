"""Tests — system checks politique IA (AI Gateway obligatoire).

Les checks sont appelés directement (les settings sont pilotés par la fixture
`settings`). On couvre : Gateway obligatoire (A), URL interdite (B), Ollama direct
interdit (C), fallback provider (D), et l'échappatoire de tests.
"""
from apps.llm_router.checks import (
    check_ai_gateway_base_url,
    check_ai_gateway_fallback_provider,
    check_ai_gateway_mandatory,
    check_no_direct_ollama,
)


def _conform(settings, env="dev"):
    """Pose une configuration conforme à la policy."""
    settings.DJANGO_ENV = env
    settings.ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = False
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://ai-gateway:8100"
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "none"
    settings.RAG_LLM_PROVIDER = "ollama"


# ============================ Check A ======================================

def test_A_conform_no_error(settings):
    _conform(settings)
    assert check_ai_gateway_mandatory(None) == []


def test_A_gateway_off_official_is_error(settings):
    _conform(settings)
    settings.AI_GATEWAY_ENABLED = False
    errors = check_ai_gateway_mandatory(None)
    assert len(errors) == 1 and errors[0].id == "llm_router.E001"


def test_A_gateway_off_tolerated_is_warning(settings):
    _conform(settings)
    settings.AI_GATEWAY_ENABLED = False
    settings.ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = True
    warns = check_ai_gateway_mandatory(None)
    assert len(warns) == 1 and warns[0].id == "llm_router.W001"


def test_A_non_official_env_skipped(settings):
    _conform(settings, env="base")
    settings.AI_GATEWAY_ENABLED = False
    assert check_ai_gateway_mandatory(None) == []


# ============================ Check B ======================================

def test_B_good_url_no_error(settings):
    _conform(settings)
    assert check_ai_gateway_base_url(None) == []


def test_B_loopback_url_is_error(settings):
    _conform(settings)
    settings.AI_GATEWAY_BASE_URL = "http://127.0.0.1:8100"
    errors = check_ai_gateway_base_url(None)
    assert len(errors) == 1 and errors[0].id == "llm_router.E002"


def test_B_localhost_and_zero_host_are_errors(settings):
    _conform(settings)
    for bad in ("http://localhost:8100", "http://0.0.0.0:8100"):
        settings.AI_GATEWAY_BASE_URL = bad
        errors = check_ai_gateway_base_url(None)
        assert len(errors) == 1 and errors[0].id == "llm_router.E002", bad


def test_B_empty_url_when_enabled_is_error(settings):
    _conform(settings)
    settings.AI_GATEWAY_BASE_URL = ""
    errors = check_ai_gateway_base_url(None)
    assert len(errors) == 1 and errors[0].id == "llm_router.E002"


def test_B_skipped_when_gateway_disabled(settings):
    # Gateway OFF => Check A gère ; B ne double-signale pas.
    _conform(settings)
    settings.AI_GATEWAY_ENABLED = False
    settings.AI_GATEWAY_BASE_URL = "http://127.0.0.1:8100"
    assert check_ai_gateway_base_url(None) == []


# ============================ Check C ======================================

def test_C_ollama_behind_active_gateway_ok(settings):
    """Gateway active : la valeur `ollama` est sans effet, le check est muet."""
    _conform(settings)
    assert check_no_direct_ollama(None) == []


def test_C_ollama_provider_official_is_error(settings):
    _conform(settings)
    settings.AI_GATEWAY_ENABLED = False
    settings.RAG_LLM_PROVIDER = "ollama"
    errors = check_no_direct_ollama(None)
    assert len(errors) == 1 and errors[0].id == "llm_router.E003"


def test_C_ollama_tolerated_is_warning(settings):
    _conform(settings)
    settings.AI_GATEWAY_ENABLED = False
    settings.RAG_LLM_PROVIDER = "ollama"
    settings.ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = True
    warns = check_no_direct_ollama(None)
    assert len(warns) == 1 and warns[0].id == "llm_router.W002"


# ============================ Check D ======================================

def test_D_none_provider_ok(settings):
    _conform(settings)
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "none"
    assert check_ai_gateway_fallback_provider(None) == []


def test_D_ollama_fallback_is_error(settings):
    _conform(settings)
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "ollama"
    errors = check_ai_gateway_fallback_provider(None)
    assert len(errors) == 1 and errors[0].id == "llm_router.E004"


def test_D_external_provider_is_error_even_when_tolerated(settings):
    """Aucun fournisseur externe n'existe : l'échappatoire de tests ne l'ouvre pas."""
    _conform(settings)
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "openai"
    settings.ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = True
    errors = check_ai_gateway_fallback_provider(None)
    assert len(errors) == 1 and errors[0].id == "llm_router.E004"
