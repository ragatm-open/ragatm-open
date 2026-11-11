"""Gouvernance du catalogue au niveau API : refus AVANT tout effet de bord.

Le défaut que ces tests verrouillent est un problème d'**ordre**, pas de règle. Avant
ce lot, `bge-m3` était accepté à la configuration, marquait tous les documents de
l'espace `reindex_required`, puis échouait à la première ingestion sur un écart de
dimension. L'espace se retrouvait avec un leg vectoriel vide et aucune réindexation
capable d'aboutir — un état que l'utilisateur avait provoqué en croyant configurer.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.llm_router import model_catalog as mc
from apps.rag_core.models import Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Membership, Workspace

from tests_root.unit.test_model_catalog import GATEWAY_MODELS, GATEWAY_RUNTIME, EXTRA_GATEWAY_MODELS

User = get_user_model()
H = {"HTTP_HOST": "localhost"}
pytestmark = pytest.mark.django_db


@pytest.fixture
def gouverne(monkeypatch, settings):
    """Catalogue actif, passerelle gouvernée exposée — sans réseau."""
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "fr-en-retrieval"
    settings.RAG_EMBEDDING_DIM = 768
    mc.invalidate_catalog_cache()
    monkeypatch.setattr(mc, "catalog_applicable", lambda: True)
    monkeypatch.setattr(mc, "gateway_models", lambda **kw: (list(GATEWAY_MODELS), False))
    monkeypatch.setattr(mc, "gateway_runtime_models", lambda **kw: (dict(GATEWAY_RUNTIME), False))
    yield
    mc.invalidate_catalog_cache()


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="MCAT-WS")
    su = User.objects.create_user("mcat_su", "mcat_su@t.local", "x",
                                  is_superuser=True, is_staff=True)
    member = User.objects.create_user("mcat_m", "mcat_m@t.local", "x")
    Membership.objects.create(workspace=ws, user=su, role="admin")
    Membership.objects.create(workspace=ws, user=member, role="owner")
    return {"ws": ws, "su": su, "member": member}


def _cli(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


def _create(u, ws, body):
    return _cli(u).post(f"/api/v1/workspaces/{ws.id}/ragspaces/", body, format="json", **H)


def _patch(u, rs, body):
    return _cli(u).patch(f"/api/v1/rag-spaces/{rs.id}/", body, format="json", **H)


@pytest.fixture
def espace(world):
    rs = RagSpace.objects.create(
        workspace=world["ws"], name="MCAT",
        llm_provider="ollama", llm_model="qwen3:30b-instruct",
        embedding_provider="ollama", embedding_model="nomic-embed-text",
    )
    for i in range(3):
        Document.objects.create(rag_space=rs, filename=f"d{i}.txt",
                                status=DocumentStatus.READY,
                                embedding_provider="ollama",
                                embedding_model="nomic-embed-text")
    return rs


# ---------------------------------------------------------------------------
# Endpoint de catalogue — §45
# ---------------------------------------------------------------------------

def test_l_endpoint_rend_les_deux_familles(world, gouverne):
    r = _cli(world["member"]).get("/api/v1/ai/model-catalog/", **H)
    assert r.status_code == 200
    assert {e["id"] for e in r.data["chat"]} >= {"qwen3:30b-instruct", "llama3.1:8b"}
    assert {e["id"] for e in r.data["embeddings"]} >= {"legacy-768", "fr-en-retrieval"}
    assert r.data["expected_embedding_dimension"] == 768
    assert r.data["qualification_revision"]
    assert r.data["can_edit_model_configuration"] is False


def test_l_endpoint_signale_l_indisponibilite(world, monkeypatch, settings):
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    monkeypatch.setattr(mc, "catalog_applicable", lambda: True)
    monkeypatch.setattr(mc, "gateway_models", lambda **kw: (None, False))
    r = _cli(world["member"]).get("/api/v1/ai/model-catalog/", **H)
    assert r.status_code == 503
    assert r.data["error"] == "model_catalog_unavailable"


def test_le_catalogue_ne_fuit_ni_digest_ni_url(world, gouverne):
    r = _cli(world["su"]).get("/api/v1/ai/model-catalog/", **H)
    blob = repr(r.data)
    assert "1a37926bf842" not in blob and "gw.test" not in blob


# ---------------------------------------------------------------------------
# Création — §48
# ---------------------------------------------------------------------------

def test_creation_avec_modele_chat_valide(world, gouverne):
    r = _create(world["su"], world["ws"],
                {"name": "A", "llm_model": "qwen3:30b-instruct"})
    assert r.status_code == 201, r.data


def test_creation_avec_faute_de_frappe_refusee(world, gouverne):
    """§20 : le refus tombe à la création, pas au premier chat."""
    r = _create(world["su"], world["ws"],
                {"name": "B", "llm_model": "qwen3:30b-instrcut"})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_UNKNOWN_MODEL
    assert not RagSpace.objects.filter(name="B").exists()


def test_creation_avec_modele_d_embedding_en_chat_refusee(world, gouverne):
    r = _create(world["su"], world["ws"],
                {"name": "C", "llm_model": "nomic-embed-text:latest"})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_WRONG_CAPABILITY


def test_creation_avec_pipeline_gouverne_valide(world, gouverne):
    r = _create(world["su"], world["ws"],
                {"name": "D", "embedding_provider": "ai_gateway",
                 "embedding_model": "fr-en-retrieval"})
    assert r.status_code == 201, r.data




def test_creation_avec_pipeline_indisponible_sur_cet_environnement(world, gouverne, settings):
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = False
    r = _create(world["su"], world["ws"],
                {"name": "F", "embedding_provider": "ai_gateway",
                 "embedding_model": "fr-en-retrieval"})
    assert r.status_code == 400
    assert r.data["code"] in ("governed_embeddings_disabled", "gateway_embeddings_not_enabled")


# ---------------------------------------------------------------------------
# Le point crucial : AUCUN effet de bord sur refus — §18, §49
# ---------------------------------------------------------------------------

def test_patch_invalide_ne_touche_ni_l_espace_ni_ses_documents(world, gouverne, espace):
    """Le cas `bge-m3`, dans son ordre d'origine : la validation doit précéder l'écriture."""
    avant = {
        "provider": espace.embedding_provider,
        "model": espace.embedding_model,
        "reindex": Document.objects.filter(rag_space=espace, reindex_required=True).count(),
        "docs": Document.objects.filter(rag_space=espace).count(),
    }
    r = _patch(world["su"], espace, {"embedding_model": "bge-m3"})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_DIMENSION
    assert r.data["field"] == "embedding_model"

    espace.refresh_from_db()
    assert espace.embedding_provider == avant["provider"]
    assert espace.embedding_model == avant["model"]
    assert Document.objects.filter(rag_space=espace, reindex_required=True).count() == avant["reindex"] == 0
    assert Document.objects.filter(rag_space=espace).count() == avant["docs"]


def test_patch_valide_passe(world, gouverne, espace):
    r = _patch(world["su"], espace,
               {"embedding_provider": "ai_gateway", "embedding_model": "fr-en-retrieval"})
    assert r.status_code == 200, r.data
    assert Document.objects.filter(rag_space=espace, reindex_required=True).count() == 3


# ---------------------------------------------------------------------------
# Espaces existants — §23
# ---------------------------------------------------------------------------

def test_un_espace_historique_reste_renommable(world, gouverne, espace):
    """Le catalogue s'applique aux CHAMPS QUI CHANGENT, pas à l'état complet.

    Sans cette règle, un espace portant une valeur que le catalogue ne connaît pas
    deviendrait impossible à renommer — invalidé par une règle arrivée après lui.
    """
    espace.llm_model = "un-modele-disparu-du-catalogue"
    espace.save(update_fields=["llm_model"])
    r = _patch(world["su"], espace, {"name": "MCAT renommé"})
    assert r.status_code == 200, r.data
    espace.refresh_from_db()
    assert espace.name == "MCAT renommé"
    assert espace.llm_model == "un-modele-disparu-du-catalogue", "valeur historique préservée"


# ---------------------------------------------------------------------------
# Échappatoire `force` — §41-42
# ---------------------------------------------------------------------------

def test_force_ne_franchit_pas_une_incompatibilite(world, gouverne, espace):
    """`force` ne rend pas un modèle 1024 compatible avec un index 768."""
    r = _patch(world["su"], espace, {"embedding_model": "bge-m3", "force": True})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_DIMENSION
    espace.refresh_from_db()
    assert espace.embedding_model == "nomic-embed-text"
    assert Document.objects.filter(rag_space=espace, reindex_required=True).count() == 0


def test_force_franchit_la_seule_qualification(world, gouverne, espace, monkeypatch):
    """Un pipeline gouverné compatible mais NON MESURÉ : refusé, sauf `force` explicite."""
    from apps.llm_router import qualification

    monkeypatch.setattr(qualification, "QUALIFIED_EMBEDDING_PIPELINES", {})
    monkeypatch.setattr(mc, "embedding_qualification", lambda m: None)

    r = _patch(world["su"], espace,
               {"embedding_provider": "ai_gateway", "embedding_model": "fr-en-retrieval"})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_NOT_QUALIFIED
    espace.refresh_from_db()
    assert espace.embedding_provider == "ollama", "aucun effet de bord sur refus"

    r = _patch(world["su"], espace,
               {"embedding_provider": "ai_gateway", "embedding_model": "fr-en-retrieval",
                "force": True})
    assert r.status_code == 200, r.data
    espace.refresh_from_db()
    assert espace.embedding_provider == "ai_gateway"


# ---------------------------------------------------------------------------
# Défauts d'environnement — §25
# ---------------------------------------------------------------------------

def test_les_defauts_d_environnement_sont_admissibles(world, gouverne, settings):
    """Gate du §25 : `default ∈ allowed`. Un défaut refusé par le catalogue rendrait
    toute création impossible pour un non-superuser, qui les reçoit imposés."""
    assert mc.validate_chat_model(settings.DEFAULT_RAGSPACE_CHAT_MODEL) is None
    assert mc.validate_embedding_model(
        settings.DEFAULT_RAGSPACE_EMBEDDING_PROVIDER,
        settings.DEFAULT_RAGSPACE_EMBEDDING_MODEL) is None
    r = _create(world["member"], world["ws"], {"name": "DEF"})
    assert r.status_code == 201, r.data
