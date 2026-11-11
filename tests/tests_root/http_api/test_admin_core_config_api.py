"""Tests PR-A — Admin RagATM_Core, endpoints lecture seule (superuser only).

Couvre : defaults sûrs, redaction base_url, read-only (pas de PATCH/POST/DELETE),
permissions (non-auth / non-superuser / superuser), et absence de secret en réponse.
NB : APPEND_SLASH=False => slash final requis.
"""
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
HEALTH = "/api/v1/admin/core-config/health/"


def _by_key(payload):
    return {e["key"]: e for e in payload["config"]}


# --- defaults sûrs (test 1) ------------------------------------------------

@pytest.mark.django_db
def test_effective_returns_safe_defaults(superuser_client, settings):
    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    settings.AI_GATEWAY_ENABLED = False
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.RAG_EMBEDDING_DIM = 768

    resp = superuser_client.get(EFFECTIVE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["read_only"] is True
    assert "environment" in body
    cfg = _by_key(body)
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["effective_value"] is False
    assert cfg["AI_GATEWAY_ENABLED"]["effective_value"] is False
    assert cfg["AI_GATEWAY_FALLBACK_ENABLED"]["effective_value"] is True
    assert cfg["RAG_EMBEDDING_DIM"]["effective_value"] == 768
    # tout est non éditable en PR-A
    assert all(e["editable"] is False for e in body["config"])
    assert all(e["source"] == "settings" for e in body["config"])


# --- redaction (test 2) ----------------------------------------------------

@pytest.mark.django_db
def test_base_url_is_redacted(superuser_client, settings):
    settings.AI_GATEWAY_BASE_URL = "http://198.51.100.5:8100"
    resp = superuser_client.get(EFFECTIVE)
    cfg = _by_key(resp.json())
    entry = cfg["AI_GATEWAY_BASE_URL"]
    assert entry["sensitive"] is True
    assert entry["redacted"] is True
    # hôte masqué, port conservé, jamais l'IP en clair
    assert entry["effective_value"] == "http://…:8100"
    assert "198.51.100.5" not in str(resp.json())


@pytest.mark.django_db
def test_empty_base_url_not_marked_redacted(superuser_client, settings):
    settings.AI_GATEWAY_BASE_URL = ""
    cfg = _by_key(superuser_client.get(EFFECTIVE).json())
    assert cfg["AI_GATEWAY_BASE_URL"]["effective_value"] == ""
    assert cfg["AI_GATEWAY_BASE_URL"]["redacted"] is False


# --- read-only (test 3 + 9) ------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("verb", ["post", "patch", "delete", "put"])
def test_effective_is_read_only(superuser_client, verb):
    resp = getattr(superuser_client, verb)(EFFECTIVE, {}, format="json")
    assert resp.status_code == 405  # méthode non autorisée


@pytest.mark.django_db
@pytest.mark.parametrize("verb", ["post", "patch", "delete", "put"])
def test_health_is_read_only(superuser_client, verb):
    resp = getattr(superuser_client, verb)(HEALTH, {}, format="json")
    assert resp.status_code == 405


# --- permissions (tests 4-8) ----------------------------------------------

@pytest.mark.django_db
def test_effective_rejects_unauthenticated(client):
    assert client.get(EFFECTIVE).status_code in (401, 403)


@pytest.mark.django_db
def test_effective_rejects_non_superuser(api_client):
    # api_client = membre de workspace, authentifié mais non superuser
    assert api_client.get(EFFECTIVE).status_code == 403


@pytest.mark.django_db
def test_effective_rejects_workspace_owner(admin_client):
    # admin_client = owner de workspace, mais PAS superuser global
    assert admin_client.get(EFFECTIVE).status_code == 403


@pytest.mark.django_db
def test_effective_accepts_superuser(superuser_client):
    assert superuser_client.get(EFFECTIVE).status_code == 200


@pytest.mark.django_db
def test_health_rejects_non_superuser(api_client):
    assert api_client.get(HEALTH).status_code == 403


@pytest.mark.django_db
def test_health_accepts_superuser_and_never_claims_ok_without_proof(superuser_client, settings, monkeypatch):
    # plus jamais « ok » sans preuve. La santé Gateway vient des sondes
    # GET /health/* (ici simulées) ; le cœur n'est pas sondé → ok = null, verified = false.
    from apps.rag_core.services.capabilities import gateway_health

    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw:8100"
    monkeypatch.setattr(gateway_health, "snapshot", lambda refresh=False: {
        "services": {"vision": {"status": "AVAILABLE"}, "asr": {"status": "UNAVAILABLE", "reason": "timeout"}},
        "probed_at": 1.0, "cached": False})
    resp = superuser_client.get(HEALTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["core_health"]["ok"] is None and body["core_health"]["verified"] is False
    assert body["core_health"]["embedding_dim"] == 768
    assert body["gateway_configured"] is True
    assert body["gateway_enabled"] is True
    assert body["gateway_health"]["status"] == "UNAVAILABLE"          # le pire des services sondés
    assert body["gateway_health"]["services"] == {"vision": "AVAILABLE", "asr": "UNAVAILABLE"}
    assert "gw:8100" not in str(body)                                  # pas de fuite d'hôte


@pytest.mark.django_db
def test_health_unknown_when_gateway_not_probed(superuser_client, monkeypatch):
    from apps.rag_core.services.capabilities import gateway_health

    monkeypatch.setattr(gateway_health, "snapshot", lambda refresh=False: {"services": {}, "probed_at": None})
    body = superuser_client.get(HEALTH).json()
    assert body["gateway_health"]["status"] == "UNKNOWN"


# --- pas de fuite de secret (test 13) -------------------------------------

@pytest.mark.django_db
def test_no_secret_or_apikey_in_response(superuser_client, settings):
    settings.AI_GATEWAY_BASE_URL = "http://secret-host.internal:8100"
    blob = str(superuser_client.get(EFFECTIVE).json()) + str(superuser_client.get(HEALTH).json())
    assert "secret-host.internal" not in blob
