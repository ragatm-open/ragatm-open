"""Administration PLATEFORME de la configuration modèle, hors périmètre owner.

Sous `OWNER_SCOPE_ENABLED=1`, deux gardes indépendantes se superposent : la politique
modèles (superuser, globale) et le périmètre owner (par espace). Leur intersection peut
être **vide** — cas réel en staging : `QUALIF-SPACE`, dont le périmètre appartient à un
compte non-superuser. Aucun utilisateur ne pouvait alors changer sa configuration par
l'API, alors que la capacité existait par commande de gestion, en shell, sans audit
applicatif.

Refuser l'API n'ajoutait aucune sécurité : elle déplaçait l'opération vers un chemin non
tracé. L'élévation est donc accordée, et strictement bornée à la configuration IA.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.audit.models import ActionAuditEvent
from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
H = {"HTTP_HOST": "localhost"}
pytestmark = pytest.mark.django_db


def _cli(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def scoped(db, settings):
    """Un espace dont le périmètre owner appartient à un NON-superuser."""
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    ws = Workspace.objects.create(name="PMA-WS")
    owner = User.objects.create_user("pma_owner", "pma_owner@t.local", "x")
    su = User.objects.create_user("pma_su", "pma_su@t.local", "x",
                                  is_superuser=True, is_staff=True)
    m = Membership.objects.create(workspace=ws, user=owner, role="owner")
    rs = RagSpace.objects.create(
        workspace=ws, name="HORS-PERIMETRE", owner_scope=m,
        llm_provider="ollama", llm_model="qwen3:30b-instruct",
        embedding_provider="ollama", embedding_model="nomic-embed-text",
    )
    # Le superuser n'est membre d'AUCUN workspace : il est hors périmètre par
    # construction, comme dans le cas staging.
    return {"ws": ws, "owner": owner, "su": su, "rs": rs}


def _patch(u, rs, body):
    return _cli(u).patch(f"/api/v1/rag-spaces/{rs.id}/", body, format="json", **H)


# ---------------------------------------------------------------------------
# L'élévation existe et est bornée
# ---------------------------------------------------------------------------

def test_le_superuser_hors_perimetre_peut_administrer_la_config_modele(scoped):
    r = _patch(scoped["su"], scoped["rs"], {"llm_model": "llama3.1:8b"})
    assert r.status_code == 200, r.data
    scoped["rs"].refresh_from_db()
    assert scoped["rs"].llm_model == "llama3.1:8b"


def test_le_superuser_hors_perimetre_peut_LIRE_la_configuration(scoped):
    """Il doit pouvoir lire ce qu'il a le droit de modifier — sinon l'écran est aveugle.

    La réponse ne porte aucun contenu documentaire : configuration et compteurs.
    """
    r = _cli(scoped["su"]).get(f"/api/v1/rag-spaces/{scoped['rs'].id}/llm-settings/", **H)
    assert r.status_code == 200
    assert r.data["embedding_model"] == "nomic-embed-text"
    assert "documents" in r.data


@pytest.mark.parametrize("body, champ", [
    ({"name": "renommé"}, "name"),
    ({"allow_external_fallback": True}, "allow_external_fallback"),
])
def test_l_elevation_ne_couvre_PAS_les_actes_metier(scoped, body, champ):
    """Renommer et ouvrir l'egress restent des actes du propriétaire du périmètre.

    L'élévation administre la configuration IA ; elle ne fait pas du superuser le
    propriétaire métier universel de l'espace.
    """
    r = _patch(scoped["su"], scoped["rs"], body)
    assert r.status_code == 403
    assert r.data["code"] == "platform_admin_scope_exceeded"
    assert r.data["fields"] == [champ]
    scoped["rs"].refresh_from_db()
    assert scoped["rs"].name == "HORS-PERIMETRE"
    assert scoped["rs"].allow_external_fallback is False


def test_l_elevation_est_auditee(scoped):
    """§22 — toute élévation effective laisse une trace nominative."""
    _patch(scoped["su"], scoped["rs"], {"llm_model": "llama3.1:8b"})
    ev = ActionAuditEvent.objects.filter(
        action_name="ragspace.model_configuration_elevated").order_by("-id").first()
    assert ev is not None
    assert str(ev.rag_space_id) == str(scoped["rs"].id)
    assert ev.metadata["reason"] == "platform_model_admin_outside_owner_scope"
    assert "llm_model" in ev.metadata["fields"]


# ---------------------------------------------------------------------------
# L'élévation ne s'applique qu'au superuser
# ---------------------------------------------------------------------------

def test_un_non_superuser_hors_perimetre_reste_refuse(scoped, db):
    etranger = User.objects.create_user("pma_etranger", "e@t.local", "x")
    r = _patch(etranger, scoped["rs"], {"llm_model": "llama3.1:8b"})
    assert r.status_code in (403, 404)
    scoped["rs"].refresh_from_db()
    assert scoped["rs"].llm_model == "qwen3:30b-instruct"


def test_le_proprietaire_du_perimetre_garde_ses_droits_metier(scoped):
    """Le chemin nominal n'est pas dégradé : l'owner renomme toujours son espace."""
    r = _patch(scoped["owner"], scoped["rs"], {"name": "renommé par l'owner"})
    assert r.status_code == 200, r.data
    scoped["rs"].refresh_from_db()
    assert scoped["rs"].name == "renommé par l'owner"


def test_le_proprietaire_ne_gagne_pas_la_config_modele(scoped):
    """La politique modèles reste superuser-only, l'élévation n'y touche pas."""
    r = _patch(scoped["owner"], scoped["rs"], {"llm_model": "llama3.1:8b"})
    assert r.status_code == 403
    assert r.data["code"] == "ragspace_model_configuration_forbidden"
