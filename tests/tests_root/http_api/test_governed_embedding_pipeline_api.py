"""Ouverture du pipeline d'embeddings GOUVERNÉ (`ai_gateway`) à l'API standard.

Le modèle Django acceptait déjà `ai_gateway` et le backend savait le traiter, mais
`ALLOWED_EMBEDDING_PROVIDERS` le refusait : un espace gouverné ne pouvait naître que
par commande de gestion. Ce lot ouvre la voie officielle — **conditionnellement**.

Le point qui décide de la valeur de ces tests : le refus doit tomber à la
CONFIGURATION. Un espace créé en `ai_gateway` sur un environnement sans Gateway
d'embeddings serait syntaxiquement valide et fonctionnellement mort — chaque document
échouerait, longtemps après que l'utilisateur ait cru avoir configuré son espace.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace, Document
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Workspace, Membership

User = get_user_model()
H = {"HTTP_HOST": "testserver"}
pytestmark = pytest.mark.django_db


def _cli(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="GEP-WS")
    su = User.objects.create_user("gep_su", "gep_su@t.local", "x",
                                  is_superuser=True, is_staff=True)
    owner = User.objects.create_user("gep_owner", "gep_owner@t.local", "x")
    Membership.objects.create(workspace=ws, user=su, role="admin")
    Membership.objects.create(workspace=ws, user=owner, role="owner")
    return {"ws": ws, "su": su, "owner": owner}


@pytest.fixture
def governed_env(settings):
    """Environnement où le pipeline gouverné est réellement exposé."""
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "fr-en-retrieval"


def _create(u, ws, body):
    return _cli(u).post(f"/api/v1/workspaces/{ws.id}/ragspaces/", body, format="json", **H)


def _patch(u, rs, body):
    return _cli(u).patch(f"/api/v1/rag-spaces/{rs.id}/", body, format="json", **H)


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------

def test_creation_gouvernee_acceptee_quand_la_gateway_est_exposee(world, governed_env):
    r = _create(world["su"], world["ws"], {
        "name": "GOV", "embedding_provider": "ai_gateway",
        "embedding_model": "fr-en-retrieval",
    })
    assert r.status_code == 201, r.data
    assert r.data["embedding_provider"] == "ai_gateway"
    assert r.data["embedding_model"] == "fr-en-retrieval"
    rs = RagSpace.objects.get(id=r.data["id"])
    assert (rs.embedding_provider, rs.embedding_model) == ("ai_gateway", "fr-en-retrieval")


def test_creation_gouvernee_refusee_si_gateway_desactivee(world, settings):
    """Refus à la configuration, pas à la première ingestion."""
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = False
    r = _create(world["su"], world["ws"], {
        "name": "GOV", "embedding_provider": "ai_gateway",
        "embedding_model": "fr-en-retrieval",
    })
    assert r.status_code == 400
    assert r.data["code"] == "governed_embeddings_disabled"
    assert not RagSpace.objects.filter(name="GOV").exists()


def test_creation_gouvernee_refusee_si_base_url_absente(world, settings):
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = ""
    r = _create(world["su"], world["ws"], {
        "name": "GOV", "embedding_provider": "ai_gateway",
        "embedding_model": "fr-en-retrieval",
    })
    assert r.status_code == 400
    assert r.data["code"] == "governed_embeddings_base_url_missing"


def test_modele_logique_requis_si_aucun_defaut_gouverne(world, settings):
    """`ai_gateway` + modèle vide n'est acceptable que si l'environnement en déclare un.

    Sans défaut, `embed_via_gateway` lèverait `model_missing` au premier document.
    """
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = ""
    r = _create(world["su"], world["ws"], {
        "name": "GOV", "embedding_provider": "ai_gateway", "embedding_model": "",
    })
    assert r.status_code == 400
    assert r.data["code"] == "governed_model_required"


def test_modele_vide_accepte_si_defaut_gouverne_declare(world, governed_env):
    r = _create(world["su"], world["ws"], {
        "name": "GOV", "embedding_provider": "ai_gateway", "embedding_model": "",
    })
    assert r.status_code == 201, r.data


def test_non_superuser_ne_peut_pas_choisir_le_pipeline_gouverne(world, governed_env):
    """La politique modèles reste superuser-only : l'ouverture n'y touche pas."""
    r = _create(world["owner"], world["ws"], {
        "name": "GOV", "embedding_provider": "ai_gateway",
        "embedding_model": "fr-en-retrieval",
    })
    assert r.status_code == 403
    assert r.data["code"] == "ragspace_model_configuration_forbidden"


# ---------------------------------------------------------------------------
# Migration d'un espace existant
# ---------------------------------------------------------------------------

@pytest.fixture
def legacy_space(world):
    rs = RagSpace.objects.create(
        workspace=world["ws"], name="LEGACY",
        embedding_provider="ollama", embedding_model="nomic-embed-text",
    )
    for i in range(3):
        Document.objects.create(rag_space=rs, filename=f"d{i}.txt",
                                status=DocumentStatus.READY,
                                embedding_provider="ollama",
                                embedding_model="nomic-embed-text")
    return rs


def test_patch_vers_gouverne_marque_les_documents_a_reindexer(world, governed_env, legacy_space):
    """Le changement d'identité vectorielle sort les documents du leg vectoriel.

    C'est la garde qui empêche de comparer des vecteurs `nomic-embed-text` à une
    question encodée par `fr-en-retrieval`.
    """
    assert Document.objects.filter(rag_space=legacy_space, reindex_required=True).count() == 0
    r = _patch(world["su"], legacy_space, {
        "embedding_provider": "ai_gateway", "embedding_model": "fr-en-retrieval",
    })
    assert r.status_code == 200, r.data
    assert r.data["embeddings_reindex_required"] is True
    legacy_space.refresh_from_db()
    assert (legacy_space.embedding_provider, legacy_space.embedding_model) == (
        "ai_gateway", "fr-en-retrieval")
    assert Document.objects.filter(rag_space=legacy_space, reindex_required=True).count() == 3


def test_patch_vers_gouverne_refuse_si_gateway_desactivee(world, settings, legacy_space):
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = False
    r = _patch(world["su"], legacy_space, {"embedding_provider": "ai_gateway"})
    assert r.status_code == 400
    assert r.data["code"] == "governed_embeddings_disabled"
    legacy_space.refresh_from_db()
    assert legacy_space.embedding_provider == "ollama", "aucun changement partiel"
    assert Document.objects.filter(rag_space=legacy_space, reindex_required=True).count() == 0


def test_patch_du_seul_modele_est_evalue_sur_l_etat_resultant(world, settings, legacy_space):
    """Un PATCH qui ne touche qu'un champ hérite de l'autre.

    L'espace est déjà gouverné ; vider le modèle sur un environnement sans défaut
    doit être refusé, alors que le payload ne parle pas du provider.
    """
    legacy_space.embedding_provider = "ai_gateway"
    legacy_space.embedding_model = "fr-en-retrieval"
    legacy_space.save(update_fields=["embedding_provider", "embedding_model"])
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = ""
    r = _patch(world["su"], legacy_space, {"embedding_model": ""})
    assert r.status_code == 400
    assert r.data["code"] == "governed_model_required"


# ---------------------------------------------------------------------------
# Le corpus métier n'est PAS ouvert par ce lot
# ---------------------------------------------------------------------------

