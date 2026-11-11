"""Tests PR-G4 — notice runtime dynamique selon CORE_CONFIG_RUNTIME_ENABLED."""
import pytest

EFFECTIVE = "/api/v1/admin/core-config/effective/"
HEALTH = "/api/v1/admin/core-config/health/"


@pytest.mark.django_db
def test_notice_runtime_off_is_diagnostic_only(superuser_client, settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    for url in (EFFECTIVE, HEALTH):
        body = superuser_client.get(url).json()
        assert body["diagnostic_overlay_only"] is True
        assert body["db_overlay_runtime_active"] is False
        assert body["runtime_source_of_truth"] == "settings"


@pytest.mark.django_db
def test_notice_runtime_on_is_active(superuser_client, settings):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True
    for url in (EFFECTIVE, HEALTH):
        body = superuser_client.get(url).json()
        assert body["diagnostic_overlay_only"] is False
        assert body["db_overlay_runtime_active"] is True
        assert body["runtime_source_of_truth"] == "db_overlay"
        assert "ACTIVE at runtime" in body["warning"]
