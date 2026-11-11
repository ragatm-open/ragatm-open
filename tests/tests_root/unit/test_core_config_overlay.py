"""Tests PR-D — overlay DB de config Core (lecture seule, DB > env, fallback env).

Garanties clés :
- table vide => identique au comportement env-only (non-régression) ;
- précédence ragspace > workspace > environment > global > settings ;
- coercion correcte des valeurs DB (texte) ;
- le RUNTIME n'est PAS modifié par un override DB (overlay diagnostic uniquement).
"""
import pytest

from apps.rag_core.core.config_resolver import resolve_effective_config


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """


pytestmark = pytest.mark.django_db

ENV = "dev"  # settings.DJANGO_ENV en test


def _by_key(environment=ENV, workspace_id=None, rag_space_id=None):
    entries = resolve_effective_config(
        environment=environment, workspace_id=workspace_id, rag_space_id=rag_space_id
    )
    return {e.key: e for e in entries}


def _mk(scope_type, scope_ref, key, value):
    from apps.rag_core.models import CoreConfigOverride

    return CoreConfigOverride.objects.create(
        scope_type=scope_type, scope_ref=scope_ref, key=key, value=value
    )


def test_empty_db_falls_back_to_settings(settings):
    settings.AI_GATEWAY_ENABLED = False
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    cfg = _by_key()
    assert all(e.source == "settings" for e in cfg.values())
    assert cfg["AI_GATEWAY_ENABLED"].effective_value is False
    assert cfg["RAG_EMBEDDING_DIM"].effective_value == 768


def test_global_override_reflected_with_source(settings):
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    _mk("global", "", "CORE_CONFIG_SELFTEST_SCOPE", "true")
    cfg = _by_key()
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"].effective_value is True
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"].source == "db:global"


def test_precedence_ragspace_beats_workspace_beats_env_beats_global(settings):
    settings.AI_GATEWAY_TIMEOUT_SECONDS = 30
    _mk("global", "", "AI_GATEWAY_TIMEOUT_SECONDS", "10")
    _mk("environment", ENV, "AI_GATEWAY_TIMEOUT_SECONDS", "20")
    _mk("workspace", "7", "AI_GATEWAY_TIMEOUT_SECONDS", "30")
    _mk("ragspace", "rs-1", "AI_GATEWAY_TIMEOUT_SECONDS", "40")

    # ragspace gagne
    assert _by_key(workspace_id=7, rag_space_id="rs-1")["AI_GATEWAY_TIMEOUT_SECONDS"].effective_value == 40
    # sans ragspace : workspace gagne
    assert _by_key(workspace_id=7)["AI_GATEWAY_TIMEOUT_SECONDS"].effective_value == 30
    # sans workspace : environment gagne
    assert _by_key()["AI_GATEWAY_TIMEOUT_SECONDS"].effective_value == 20


def test_global_used_when_no_higher_scope(settings):
    _mk("global", "", "AI_GATEWAY_TIMEOUT_SECONDS", "11")
    cfg = _by_key()
    assert cfg["AI_GATEWAY_TIMEOUT_SECONDS"].effective_value == 11
    assert cfg["AI_GATEWAY_TIMEOUT_SECONDS"].source == "db:global"


def test_bool_string_coercion(settings):
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    _mk("global", "", "AI_GATEWAY_FALLBACK_ENABLED", "false")
    assert _by_key()["AI_GATEWAY_FALLBACK_ENABLED"].effective_value is False


def test_base_url_override_is_redacted(settings):
    _mk("global", "", "AI_GATEWAY_BASE_URL", "http://198.51.100.9:9000")
    e = _by_key()["AI_GATEWAY_BASE_URL"]
    assert e.effective_value == "http://…:9000"
    assert e.redacted is True
    assert e.source == "db:global"


def test_db_override_does_not_change_runtime(settings):
    # Le runtime (router) lit les SETTINGS, pas l'overlay : un override DB ne doit
    # PAS activer le Gateway. L'overlay est diagnostic uniquement en PR-D.
    import apps.llm_router.router as router

    settings.AI_GATEWAY_ENABLED = False
    _mk("global", "", "AI_GATEWAY_ENABLED", "true")

    assert router._gateway_enabled() is False  # runtime inchangé
    cfg = _by_key()
    assert cfg["AI_GATEWAY_ENABLED"].effective_value is True  # mais l'overlay le reflète
    assert cfg["AI_GATEWAY_ENABLED"].source == "db:global"
