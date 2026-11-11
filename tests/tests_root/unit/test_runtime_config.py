"""Tests PR-G1 — résolveur runtime de config Core (INERTE par défaut)."""
import pytest
from django.core.cache import cache

from apps.rag_core.core import runtime_config as rc


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _ovr(scope_type, scope_ref, key, value):
    from apps.rag_core.models import CoreConfigOverride

    return CoreConfigOverride.objects.create(
        scope_type=scope_type, scope_ref=scope_ref, key=key, value=value
    )


# =========================== A. Flag off ===================================

def test_flag_off_reads_settings(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.AI_GATEWAY_ENABLED = False
    assert rc.get("AI_GATEWAY_ENABLED") is False


def test_flag_off_no_db_even_with_override(settings, django_assert_num_queries):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    _ovr("global", "", "CORE_CONFIG_SELFTEST_SCOPE", "true")
    with django_assert_num_queries(0):
        assert rc.get("CORE_CONFIG_SELFTEST_SCOPE") is False


def test_flag_off_does_not_touch_cache(settings, monkeypatch):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.AI_GATEWAY_ENABLED = False

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("cache must not be accessed when flag off")

    monkeypatch.setattr(rc.cache, "get", _boom)
    assert rc.get("AI_GATEWAY_ENABLED") is False


# =========================== B. Flag on ====================================

def test_flag_on_global_override(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    _ovr("global", "", "CORE_CONFIG_SELFTEST_SCOPE", "true")
    assert rc.get("CORE_CONFIG_SELFTEST_SCOPE") is True


def test_environment_beats_global(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.DJANGO_ENV = "dev"
    settings.AI_GATEWAY_TIMEOUT_SECONDS = 99
    _ovr("global", "", "AI_GATEWAY_TIMEOUT_SECONDS", "10")
    _ovr("environment", "dev", "AI_GATEWAY_TIMEOUT_SECONDS", "20")
    assert rc.get("AI_GATEWAY_TIMEOUT_SECONDS") == 20


def test_workspace_beats_environment(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.DJANGO_ENV = "dev"
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    _ovr("environment", "dev", "CORE_CONFIG_SELFTEST_SCOPE", "false")
    _ovr("workspace", "7", "CORE_CONFIG_SELFTEST_SCOPE", "true")
    assert rc.get("CORE_CONFIG_SELFTEST_SCOPE", workspace_id=7) is True


def test_ragspace_override_ignored_beyond_max_scope(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_ENABLED = False
    # AI_GATEWAY_ENABLED max_scope = environment => override ragspace ignoré.
    _ovr("ragspace", "rs-1", "AI_GATEWAY_ENABLED", "true")
    assert rc.get("AI_GATEWAY_ENABLED", rag_space_id="rs-1") is False


def test_fallback_settings_when_no_override(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    assert rc.get("AI_GATEWAY_FALLBACK_ENABLED") is True


# =========================== C. Typage =====================================

def test_bool_typing(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    _ovr("global", "", "CORE_CONFIG_SELFTEST_SCOPE", "true")
    v = rc.get("CORE_CONFIG_SELFTEST_SCOPE")
    assert v is True and isinstance(v, bool)


def test_int_typing(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    _ovr("global", "", "AI_GATEWAY_TIMEOUT_SECONDS", "45")
    v = rc.get("AI_GATEWAY_TIMEOUT_SECONDS")
    assert v == 45 and isinstance(v, int)


def test_str_typing(settings):
    """Typage str conservé — sur une clé texte qui reste pilotable.

    `AI_GATEWAY_BASE_URL` est sortie du périmètre éditable (durcissement P0) :
    elle sert désormais à prouver l'inverse, cf. le test suivant.
    """
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw-settings:8100"
    v = rc.get("AI_GATEWAY_BASE_URL")
    assert v == "http://gw-settings:8100" and isinstance(v, str)


def test_base_url_override_is_neutralised(settings):
    """Clé d'infrastructure : une ligne en base ne change pas la valeur runtime."""
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw-settings:8100"
    _ovr("global", "", "AI_GATEWAY_BASE_URL", "http://gw-overlay:9000")
    assert rc.get("AI_GATEWAY_BASE_URL") == "http://gw-settings:8100"


def test_invalid_db_value_falls_back_to_settings(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.AI_GATEWAY_TIMEOUT_SECONDS = 30
    _ovr("global", "", "AI_GATEWAY_TIMEOUT_SECONDS", "not-an-int")
    assert rc.get("AI_GATEWAY_TIMEOUT_SECONDS") == 30


# =========================== D. RAG_EMBEDDING_DIM exclu ====================

def test_embedding_dim_excluded_flag_on(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.RAG_EMBEDDING_DIM = 768
    _ovr("global", "", "RAG_EMBEDDING_DIM", "1024")
    assert rc.get("RAG_EMBEDDING_DIM") == 768


def test_embedding_dim_excluded_flag_off(settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.RAG_EMBEDDING_DIM = 768
    assert rc.get("RAG_EMBEDDING_DIM") == 768


# =========================== E. Cache / epoch ==============================

def test_set_override_bumps_epoch(django_user_model):
    from apps.rag_core.core.config_write import set_override

    u = django_user_model.objects.create_user(username="root", password="pw", email="r@e.com")
    before = rc.current_epoch()
    set_override(actor=u, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    assert rc.current_epoch() > before


def test_unset_override_bumps_epoch(django_user_model):
    from apps.rag_core.core.config_write import set_override, unset_override

    u = django_user_model.objects.create_user(username="root", password="pw", email="r@e.com")
    set_override(actor=u, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    before = rc.current_epoch()
    unset_override(actor=u, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE")
    assert rc.current_epoch() > before


def test_get_sees_new_value_after_write_invalidation(settings, django_user_model):
    from apps.rag_core.core.config_write import set_override

    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    # 1er get => settings False, mis en cache à l'epoch courant.
    assert rc.get("CORE_CONFIG_SELFTEST_SCOPE") is False
    u = django_user_model.objects.create_user(username="root", password="pw", email="r@e.com")
    set_override(actor=u, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    # epoch bumpé => nouvelle clé de cache => recalcul => True.
    assert rc.get("CORE_CONFIG_SELFTEST_SCOPE") is True


def test_cache_unavailable_falls_back_to_settings(settings, monkeypatch):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    _ovr("global", "", "CORE_CONFIG_SELFTEST_SCOPE", "true")

    def _boom(*a, **k):
        raise RuntimeError("cache down")

    monkeypatch.setattr(rc.cache, "get", _boom)
    # Cache indisponible => settings/default, sans exception.
    assert rc.get("CORE_CONFIG_SELFTEST_SCOPE") is False


# =========================== F. Parité settings (flag off) =================

def test_router_gateway_enabled_reads_settings(settings):
    import apps.llm_router.router as router

    settings.AI_GATEWAY_ENABLED = True
    assert router._gateway_enabled() is True
    settings.AI_GATEWAY_ENABLED = False
    assert router._gateway_enabled() is False


def test_gateway_client_from_settings_reads_settings(settings):
    from apps.llm_router.gateway import AIGatewayClient

    settings.AI_GATEWAY_BASE_URL = "http://gw-from-settings:8100"
    settings.AI_GATEWAY_TIMEOUT_SECONDS = 17
    client = AIGatewayClient.from_settings()
    assert client.base_url == "http://gw-from-settings:8100"
    assert client.timeout_s == 17


def test_flag_off_override_does_not_change_router(settings, django_user_model):
    """runtime off : un override DB n'affecte pas le router (lit settings).

    L'override porte sur une clé ÉDITABLE (`AI_GATEWAY_TIMEOUT_SECONDS`) :
    `AI_GATEWAY_ENABLED` est désormais refusée à l'écriture, ce qui prouverait
    autre chose que la parité flag-off recherchée ici.
    """
    import apps.llm_router.router as router
    from apps.rag_core.core.config_write import set_override

    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.AI_GATEWAY_ENABLED = False
    settings.AI_GATEWAY_TIMEOUT_SECONDS = 30
    u = django_user_model.objects.create_user(username="root", password="pw", email="r@e.com")
    set_override(
        actor=u, scope_type="global", scope_ref="", key="AI_GATEWAY_TIMEOUT_SECONDS", value="45"
    )
    assert router._gateway_enabled() is False
    assert rc.get("AI_GATEWAY_TIMEOUT_SECONDS") == 30
