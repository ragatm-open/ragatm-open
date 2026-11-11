"""Verrouillage de la config MODÈLES d'un RagSpace : superuser only.

Non-superuser → valeurs canoniques imposées ; toute tentative de valeur différente
est refusée (jamais appliquée). Superuser → libre. Config historique préservée.
"""
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.audit.models import ActionAuditEvent
from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db
H = {"HTTP_HOST": "localhost"}

# Valeurs canoniques lues depuis les SETTINGS, jamais figées en littéral : un test
# qui code en dur le modèle confirme le littéral au lieu de vérifier que la valeur
# suit `DEFAULT_RAGSPACE_*`. C'est exactement ce qui avait rendu invisible la
# divergence de production du 27/07/2026.
CANON_CHAT = settings.DEFAULT_RAGSPACE_CHAT_MODEL
CANON_EMB = settings.DEFAULT_RAGSPACE_EMBEDDING_MODEL


def _cli(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="MP-WS")
    owner = User.objects.create_user("mp_owner", "mp_owner@t.local", "x")  # non-superuser
    su = User.objects.create_user("mp_su", "mp_su@t.local", "x", is_superuser=True, is_staff=True)
    Membership.objects.create(workspace=ws, user=owner, role="owner")
    Membership.objects.create(workspace=ws, user=su, role="admin")
    return {"ws": ws, "owner": owner, "su": su}


def _create_url(ws):
    return f"/api/v1/workspaces/{ws.id}/ragspaces/"


# Modèle « autre » : volontairement DIFFÉRENT du défaut canonique (`llama3.1:8b` en
# édition Open), sinon les tests de refus confirmeraient une valeur identique.
OTHER_CHAT = "mistral:7b"
assert OTHER_CHAT != CANON_CHAT  # noqa: S101

# --- Création non-superuser -------------------------------------------------

def test_nonsu_create_without_models_gets_canonical(world):
    r = _cli(world["owner"]).post(_create_url(world["ws"]), {"name": "A"}, format="json", **H)
    assert r.status_code == 201
    assert r.data["llm_model"] == CANON_CHAT and r.data["embedding_model"] == CANON_EMB
    assert r.data["llm_provider"] == "ollama" and r.data["embedding_provider"] == "ai_gateway"
    assert r.data["can_edit_model_configuration"] is False


def test_nonsu_create_with_canonical_accepted(world):
    r = _cli(world["owner"]).post(_create_url(world["ws"]),
        {"name": "B", "llm_provider": "ollama", "llm_model": CANON_CHAT,
         "embedding_provider": "ai_gateway", "embedding_model": CANON_EMB}, format="json", **H)
    assert r.status_code == 201 and r.data["llm_model"] == CANON_CHAT


def test_nonsu_create_with_other_model_forbidden(world):
    r = _cli(world["owner"]).post(_create_url(world["ws"]),
        {"name": "C", "llm_model": OTHER_CHAT}, format="json", **H)
    assert r.status_code == 403 and r.data["code"] == "ragspace_model_configuration_forbidden"
    assert "llm_model" in r.data["fields"]
    assert not RagSpace.objects.filter(name="C").exists()  # aucune écriture
    # audit denied émis (append-only)
    assert ActionAuditEvent.objects.filter(action_name="ragspace.model_configuration_denied").exists()


def test_nonsu_create_with_openai_provider_forbidden(world):
    r = _cli(world["owner"]).post(_create_url(world["ws"]),
        {"name": "D", "llm_provider": "openai"}, format="json", **H)
    assert r.status_code == 403 and r.data["code"] == "ragspace_model_configuration_forbidden"


# --- Création superuser -----------------------------------------------------

def test_su_create_with_other_model_accepted(world):
    r = _cli(world["su"]).post(_create_url(world["ws"]),
        {"name": "E", "llm_model": OTHER_CHAT}, format="json", **H)
    assert r.status_code == 201 and r.data["llm_model"] == OTHER_CHAT
    assert r.data["can_edit_model_configuration"] is True


# --- Modification non-superuser (config historique préservée) ---------------

@pytest.fixture
def hist(world):
    return RagSpace.objects.create(workspace=world["ws"], name="HIST",
                                   llm_model=OTHER_CHAT, embedding_model=CANON_EMB)


def _patch(u, rs, body):
    return _cli(u).patch(f"/api/v1/rag-spaces/{rs.id}/", body, format="json", **H)


def test_nonsu_patch_name_only_ok_and_preserves_models(world, hist):
    r = _patch(world["owner"], hist, {"name": "HIST2"})
    assert r.status_code == 200
    hist.refresh_from_db()
    assert hist.name == "HIST2" and hist.llm_model == OTHER_CHAT  # modèle NON réécrit


def test_nonsu_patch_chat_model_forbidden(world, hist):
    r = _patch(world["owner"], hist, {"llm_model": CANON_CHAT})
    assert r.status_code == 403 and r.data["code"] == "ragspace_model_configuration_forbidden"
    hist.refresh_from_db()
    assert hist.llm_model == OTHER_CHAT


def test_nonsu_patch_embedding_model_forbidden(world, hist):
    r = _patch(world["owner"], hist, {"embedding_model": "text-embedding-3-small"})
    assert r.status_code == 403


def test_nonsu_patch_provider_forbidden(world, hist):
    r = _patch(world["owner"], hist, {"llm_provider": "openai"})
    assert r.status_code == 403


def test_nonsu_patch_same_value_ignored(world, hist):
    r = _patch(world["owner"], hist, {"name": "HIST3", "llm_model": OTHER_CHAT})
    assert r.status_code == 200
    hist.refresh_from_db()
    assert hist.name == "HIST3" and hist.llm_model == OTHER_CHAT


# --- Modification superuser -------------------------------------------------

def test_su_patch_model_accepted(world, hist):
    r = _patch(world["su"], hist, {"llm_model": CANON_CHAT})
    assert r.status_code == 200
    hist.refresh_from_db()
    assert hist.llm_model == CANON_CHAT
    assert ActionAuditEvent.objects.filter(action_name="ragspace.model_configuration_changed").exists()


# --- Sécurité ---------------------------------------------------------------

def test_staff_non_superuser_forbidden(world, hist):
    staff = User.objects.create_user("mp_staff", "mp_staff@t.local", "x", is_staff=True, is_superuser=False)
    Membership.objects.create(workspace=world["ws"], user=staff, role="owner")
    r = _patch(staff, hist, {"llm_model": CANON_CHAT})
    assert r.status_code == 403


def test_inactive_superuser_forbidden(world, hist):
    dead = User.objects.create_user("mp_dead", "mp_dead@t.local", "x", is_superuser=True, is_active=False)
    Membership.objects.create(workspace=world["ws"], user=dead, role="owner")
    r = _patch(dead, hist, {"llm_model": CANON_CHAT})
    assert r.status_code == 403
    hist.refresh_from_db()
    assert hist.llm_model == OTHER_CHAT
