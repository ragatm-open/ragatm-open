"""Tests PR-D — endpoint effective reflète l'overlay DB (lecture seule)."""
import pytest


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """


EFFECTIVE = "/api/v1/admin/core-config/effective/"


def _by_key(payload):
    return {e["key"]: e for e in payload["config"]}


def _mk(scope_type, scope_ref, key, value):
    from apps.rag_core.models import CoreConfigOverride

    CoreConfigOverride.objects.create(scope_type=scope_type, scope_ref=scope_ref, key=key, value=value)


@pytest.mark.django_db
def test_effective_empty_db_is_settings(superuser_client, settings):
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    cfg = _by_key(superuser_client.get(EFFECTIVE).json())
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["source"] == "settings"


@pytest.mark.django_db
def test_effective_reflects_global_override(superuser_client, settings):
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    _mk("global", "", "CORE_CONFIG_SELFTEST_SCOPE", "true")
    cfg = _by_key(superuser_client.get(EFFECTIVE).json())
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["effective_value"] is True
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["source"] == "db:global"
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["editable"] is False  # toujours lecture seule


@pytest.mark.django_db
def test_effective_workspace_scope_param(superuser_client, settings):
    settings.AI_GATEWAY_TIMEOUT_SECONDS = 30
    _mk("workspace", "42", "AI_GATEWAY_TIMEOUT_SECONDS", "99")
    # sans param : workspace non pris en compte
    base = _by_key(superuser_client.get(EFFECTIVE).json())
    assert base["AI_GATEWAY_TIMEOUT_SECONDS"]["effective_value"] == 30
    # avec workspace_id : override pris en compte
    scoped = _by_key(superuser_client.get(EFFECTIVE, {"workspace_id": "42"}).json())
    assert scoped["AI_GATEWAY_TIMEOUT_SECONDS"]["effective_value"] == 99
    assert scoped["AI_GATEWAY_TIMEOUT_SECONDS"]["source"] == "db:workspace"


@pytest.mark.django_db
def test_effective_invalid_workspace_id_400(superuser_client):
    assert superuser_client.get(EFFECTIVE, {"workspace_id": "abc"}).status_code == 400


@pytest.mark.django_db
def test_effective_carries_diagnostic_only_notice(superuser_client):
    body = superuser_client.get(EFFECTIVE).json()
    assert body["diagnostic_overlay_only"] is True
    assert body["db_overlay_runtime_active"] is False
    assert body["runtime_source_of_truth"] == "settings"
    assert "diagnostic-only" in body["warning"]


@pytest.mark.django_db
def test_health_carries_diagnostic_only_notice(superuser_client):
    body = superuser_client.get("/api/v1/admin/core-config/health/").json()
    assert body["diagnostic_overlay_only"] is True
    assert body["db_overlay_runtime_active"] is False
