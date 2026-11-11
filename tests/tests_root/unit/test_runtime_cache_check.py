"""Tests — system check : runtime overlay activé exige un cache partagé.

On appelle le check directement (les settings sont chargés une fois). L'id attendu
est rag_core.E003 (E001/E002 sont déjà pris par le check de dimension d'embeddings).
"""
from apps.rag_core.apps import check_runtime_config_cache


def test_no_error_when_runtime_off(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.DJANGO_CACHE_URL = ""
    assert check_runtime_config_cache(None) == []


def test_no_error_when_runtime_on_with_cache(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.DJANGO_CACHE_URL = "redis://redis:6379/2"
    assert check_runtime_config_cache(None) == []


def test_error_when_runtime_on_without_cache(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.DJANGO_CACHE_URL = ""
    errors = check_runtime_config_cache(None)
    assert len(errors) == 1
    assert errors[0].id == "rag_core.E003"
    assert "CORE_CONFIG_RUNTIME_ENABLED is enabled" in errors[0].msg
    assert "DJANGO_CACHE_URL=redis://redis:6379/2" in (errors[0].hint or "")


def test_error_when_cache_url_is_whitespace(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.DJANGO_CACHE_URL = "   "
    errors = check_runtime_config_cache(None)
    assert len(errors) == 1 and errors[0].id == "rag_core.E003"


def test_defaults_produce_no_error(settings):
    # Defaults actuels : runtime off => aucun blocage.
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    assert check_runtime_config_cache(None) == []
