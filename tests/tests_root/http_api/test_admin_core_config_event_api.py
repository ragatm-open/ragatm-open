"""Tests PR-E — endpoint lecture seule du journal CoreConfigAuditEvent."""
import pytest


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """


EP = "/api/v1/admin/core-config/config-audit/"


def _mk(**kw):
    from apps.rag_core.core.config_audit import record_config_event

    return record_config_event(**kw)


# --- permissions / read-only ----------------------------------------------

@pytest.mark.django_db
def test_rejects_unauthenticated(client):
    assert client.get(EP).status_code in (401, 403)


@pytest.mark.django_db
def test_rejects_non_superuser(api_client):
    assert api_client.get(EP).status_code == 403


@pytest.mark.django_db
def test_accepts_superuser_empty(superuser_client):
    body = superuser_client.get(EP).json()
    assert body["count"] == 0
    assert body["results"] == []
    # garde-fou diagnostic présent
    assert body["diagnostic_overlay_only"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("verb", ["post", "patch", "delete", "put"])
def test_read_only(superuser_client, verb):
    assert getattr(superuser_client, verb)(EP, {}, format="json").status_code == 405


# --- contenu + redaction ---------------------------------------------------

@pytest.mark.django_db
def test_lists_event(superuser_client, superuser):
    _mk(action="set", key="CORE_CONFIG_SELFTEST_SCOPE", scope_type="global",
        old_value="false", new_value="true", reason="enable", actor=superuser)
    body = superuser_client.get(EP).json()
    assert body["count"] == 1
    ev = body["results"][0]
    assert ev["action"] == "set"
    assert ev["key"] == "CORE_CONFIG_SELFTEST_SCOPE"
    assert ev["new_value"] == "true"
    assert ev["actor"] == "root"
    assert ev["redacted"] is False


@pytest.mark.django_db
def test_sensitive_value_is_redacted(superuser_client, superuser):
    _mk(action="set", key="AI_GATEWAY_BASE_URL", scope_type="global",
        old_value="", new_value="http://198.51.100.7:8100", actor=superuser)
    body = superuser_client.get(EP).json()
    ev = body["results"][0]
    assert ev["redacted"] is True
    assert ev["new_value"] == "http://…:8100"
    assert "198.51.100.7" not in str(body)


@pytest.mark.django_db
def test_does_not_leak_actor_email(superuser_client, superuser):
    _mk(action="set", key="X", new_value="1", actor=superuser)
    assert superuser.email not in str(superuser_client.get(EP).json())
