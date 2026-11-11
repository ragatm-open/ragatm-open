"""Tests de l'endpoint GET /api/v1/capabilities/.

Contrat :
- accessible à tout utilisateur authentifié (PAS réservé owner/admin) ;
- expose le provider d'embeddings GLOBAL (indexation), distinct du chat/RagSpace ;
- reflète EMBEDDINGS_PROVIDER (ne mentionne plus Ollama si OpenAI, et inversement) ;
- ne fuit aucun secret (clé API, URL interne).
"""
import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_requires_authentication():
    client = APIClient()
    res = client.get("/api/v1/capabilities/")
    assert res.status_code in (401, 403)


def test_accessible_to_plain_member(api_client):
    # api_client = simple "member" (pas owner/admin) -> doit pouvoir lire.
    res = api_client.get("/api/v1/capabilities/")
    assert res.status_code == 200
    body = res.json()
    assert "embeddings" in body
    # L'endpoint global expose le réglage d'embeddings GLOBAL (repli d'affichage).
    assert body["embeddings"]["scope"] == "global"
    assert "provider" in body["embeddings"]
    # Capacités providers présentes : moteur local + pipeline gouverné (aucun fournisseur externe).
    assert body["providers"]["ai_gateway"]["available"] in (True, False)
    assert "openai" not in body["providers"]


def test_provider_ai_gateway(api_client, settings):
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    res = api_client.get("/api/v1/capabilities/")
    emb = res.json()["embeddings"]
    assert emb["provider"] == "ai_gateway"


def test_no_secret_leaked(api_client, settings):
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    settings.SECRET_KEY = "sk-super-secret-key"
    settings.OLLAMA_BASE_URL = "http://internal-host:11434"
    res = api_client.get("/api/v1/capabilities/")
    raw = res.content.decode()
    assert "sk-super-secret-key" not in raw
    assert "internal-host" not in raw


# ---------------------------------------------------------------------------
# `ingest` — contrat de formats consommé par le frontend (lot MSG + formats
# dynamiques). Le frontend n'a plus AUCUNE allowlist de runtime : tout ce qu'il
# propose vient d'ici.
# ---------------------------------------------------------------------------


def test_ingest_announces_formats_and_image_subformats(api_client):
    body = api_client.get("/api/v1/capabilities/").json()
    ingest = body["ingest"]
    assert isinstance(ingest["formats"], list)
    # La FAMILLE `image` ne suffit pas : le frontend doit savoir LESQUELS.
    assert isinstance(ingest["image_formats"], list)
    if "image" in ingest["formats"]:
        assert ingest["image_formats"], "famille image annoncée sans aucun sous-format"


def test_image_formats_follow_the_server_allowlist(api_client, settings):
    settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
    # Le réglage est consommé comme une chaîne séparée par des virgules (valeur
    # d'environnement), même si le registre le déclare `list` — écart préexistant.
    settings.DOCUMENT_IMAGE_FORMATS = "jpeg,png"
    ingest = api_client.get("/api/v1/capabilities/").json()["ingest"]
    assert ingest["image_formats"] == ["jpeg", "png"]
    assert "webp" not in ingest["image_formats"]


def test_image_formats_empty_when_image_gate_closed(api_client, settings):
    """Une famille annoncée ne doit jamais survivre à son propre interrupteur."""
    settings.DOCUMENT_IMAGE_INGEST_ENABLED = False
    ingest = api_client.get("/api/v1/capabilities/").json()["ingest"]
    assert ingest["image_formats"] == []


def test_msg_absent_from_formats_when_flag_closed(api_client, settings):
    settings.DOCUMENT_MSG_ENABLED = False
    ingest = api_client.get("/api/v1/capabilities/").json()["ingest"]
    assert "msg" not in ingest["formats"]
    assert ingest["msg"]["status"] == "disabled_by_config"


def test_msg_stays_unsupported_even_when_flag_open(api_client, settings):
    """Édition Open : aucun parseur .msg (dépendance GPL écartée) — le flag n'ouvre rien."""
    settings.DOCUMENT_MSG_ENABLED = True
    ingest = api_client.get("/api/v1/capabilities/").json()["ingest"]
    assert "msg" not in ingest["formats"]
    assert ingest["msg"]["status"] == "unsupported"


def test_dwg_never_announced_while_flag_closed(api_client, settings):
    settings.DOCUMENT_DWG_ENABLED = False
    ingest = api_client.get("/api/v1/capabilities/").json()["ingest"]
    assert "dwg" not in ingest["formats"]
