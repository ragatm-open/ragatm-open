"""Validation API des modèles de génération et du moteur de chat (GATE-2).

Le sélecteur n'est pas une barrière : un administrateur peut écrire directement dans
l'API. Ces tests verrouillent l'invariant

    validation à la CRÉATION == validation à la MODIFICATION

pour la capacité de génération et le moteur, et le corollaire « espace historique
préservé » : seule une valeur qui CHANGE est jugée.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.llm_router import model_catalog as mc
from apps.rag_core.models import Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Membership, Workspace

from tests_root.unit.test_model_catalog import EXTRA_GATEWAY_MODELS, GATEWAY_MODELS, GATEWAY_RUNTIME

User = get_user_model()
H = {"HTTP_HOST": "localhost"}
pytestmark = pytest.mark.django_db

INCOMPATIBLES = [
    ("gpt-oss:20b", mc.ERR_GENERATION_UNSUPPORTED),
    ("qwen3:30b-a3b-thinking-2507-q4_K_M", mc.ERR_GENERATION_UNSUPPORTED),
    ("semx-minicpm-v4.5:8b-q8_0", mc.ERR_OUTPUT_POLICY),
    ("deepseek-r1:32b", mc.ERR_GENERATION_NOT_VERIFIED),
    ("modele-inconnu:1b", mc.ERR_UNKNOWN_MODEL),
]


@pytest.fixture
def gw(monkeypatch, settings):
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    settings.DEFAULT_RAGSPACE_CHAT_MODEL = "qwen3:30b-instruct"  # modèle servi par la fausse Gateway
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "legacy-768"
    settings.RAG_EMBEDDING_DIM = 768
    state = {"models": list(GATEWAY_MODELS) + list(EXTRA_GATEWAY_MODELS),
             "runtime": dict(GATEWAY_RUNTIME)}
    mc.invalidate_catalog_cache()
    monkeypatch.setattr(mc, "catalog_applicable", lambda: True)
    monkeypatch.setattr(mc, "gateway_models", lambda **kw: (state["models"], False))
    monkeypatch.setattr(mc, "gateway_runtime_models", lambda **kw: (state["runtime"], False))
    yield state
    mc.invalidate_catalog_cache()


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="GATE2-WS")
    su = User.objects.create_user("g2_su", "g2_su@t.local", "x", is_superuser=True, is_staff=True)
    member = User.objects.create_user("g2_m", "g2_m@t.local", "x")
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
        workspace=world["ws"], name="G2", llm_provider="ollama", llm_model="qwen3:30b-instruct",
        embedding_provider="ai_gateway", embedding_model="legacy-768",
    )
    for i in range(2):
        Document.objects.create(rag_space=rs, filename=f"d{i}.txt", status=DocumentStatus.READY,
                                embedding_provider="ai_gateway", embedding_model="legacy-768")
    return rs


def _snapshot(rs):
    rs.refresh_from_db()
    return (rs.name, rs.llm_provider, rs.llm_model, rs.embedding_provider, rs.embedding_model,
            Document.objects.filter(rag_space=rs).count(),
            Document.objects.filter(rag_space=rs, reindex_required=True).count())


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------

def test_creation_qwen3_acceptee(world, gw):
    r = _create(world["su"], world["ws"], {"name": "OK", "llm_provider": "ollama",
                                           "llm_model": "qwen3:30b-instruct"})
    assert r.status_code == 201, r.data
    assert r.data["llm_model"] == "qwen3:30b-instruct"


@pytest.mark.parametrize("model, code", INCOMPATIBLES)
def test_creation_avec_modele_non_servi_refusee(world, gw, model, code):
    r = _create(world["su"], world["ws"], {"name": f"X-{model}", "llm_model": model})
    assert r.status_code == 400
    assert r.data["code"] == code and r.data["field"] == "llm_model"
    assert "compatible" in r.data["detail"] or "catalogue" in r.data["detail"]
    assert not RagSpace.objects.filter(name=f"X-{model}").exists()


def test_creation_avec_modele_devenu_indisponible_refusee(world, gw):
    """Le catalogue du navigateur n'est pas une preuve : c'est l'état AU MOMENT de
    l'enregistrement qui compte."""
    gw["models"] = [m for m in gw["models"] if m["name"] != "mistral-small3.2:24b"]
    r = _create(world["su"], world["ws"], {"name": "GONE", "llm_model": "mistral-small3.2:24b"})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_UNKNOWN_MODEL


def test_creation_registre_illisible_refusee(world, gw):
    gw["runtime"] = None
    r = _create(world["su"], world["ws"], {"name": "NR", "llm_model": "qwen3:30b-instruct"})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_GENERATION_NOT_VERIFIED


# ---------------------------------------------------------------------------
# Modification — même règle
# ---------------------------------------------------------------------------

def test_modification_vers_modele_compatible_acceptee(world, gw, espace):
    r = _patch(world["su"], espace, {"llm_model": "mistral-small3.2:24b"})
    assert r.status_code == 200, r.data
    espace.refresh_from_db()
    assert espace.llm_model == "mistral-small3.2:24b"
    assert r.data["embeddings_reindex_required"] is False
    assert Document.objects.filter(rag_space=espace, reindex_required=True).count() == 0


@pytest.mark.parametrize("model, code", INCOMPATIBLES)
def test_modification_vers_modele_non_servi_refusee(world, gw, espace, model, code):
    avant = _snapshot(espace)
    r = _patch(world["su"], espace, {"llm_model": model})
    assert r.status_code == 400 and r.data["code"] == code and r.data["field"] == "llm_model"
    assert _snapshot(espace) == avant, "aucun effet de bord"


@pytest.mark.parametrize("model, _code", INCOMPATIBLES)
def test_creation_et_modification_jugent_pareil(world, gw, espace, model, _code):
    c = _create(world["su"], world["ws"], {"name": f"Y-{model}", "llm_model": model})
    p = _patch(world["su"], espace, {"llm_model": model})
    assert (c.status_code, c.data["code"]) == (p.status_code, p.data["code"])


# ---------------------------------------------------------------------------
# Espaces historiques
# ---------------------------------------------------------------------------

@pytest.fixture
def historique(world):
    """Espace créé avant le durcissement avec un modèle aujourd'hui non servi."""
    rs = RagSpace.objects.create(
        workspace=world["ws"], name="LEGACY", llm_provider="ollama", llm_model="gpt-oss:20b",
        embedding_provider="ai_gateway", embedding_model="legacy-768",
    )
    Document.objects.create(rag_space=rs, filename="l.txt", status=DocumentStatus.READY,
                            embedding_provider="ai_gateway", embedding_model="legacy-768")
    return rs


def test_historique_renommable_sans_changer_de_modele(world, gw, historique):
    r = _patch(world["su"], historique, {"name": "LEGACY renommé"})
    assert r.status_code == 200, r.data
    historique.refresh_from_db()
    assert historique.llm_model == "gpt-oss:20b", "jamais remplacé en silence"


def test_historique_renvoi_du_meme_modele_accepte(world, gw, historique):
    """Le formulaire renvoie les quatre champs : un modèle INCHANGÉ n'est pas jugé."""
    r = _patch(world["su"], historique, {"name": "LEGACY 2", "llm_provider": "ollama",
                                         "llm_model": "gpt-oss:20b",
                                         "embedding_provider": "ai_gateway",
                                         "embedding_model": "legacy-768"})
    assert r.status_code == 200, r.data
    assert _snapshot(historique)[2] == "gpt-oss:20b"


def test_historique_peut_choisir_un_modele_valide(world, gw, historique):
    r = _patch(world["su"], historique, {"llm_model": "qwen3:30b-instruct"})
    assert r.status_code == 200, r.data
    assert Document.objects.filter(rag_space=historique, reindex_required=True).count() == 0


def test_historique_signale_dans_les_reglages(world, gw, historique):
    """L'état est lisible : valeur persistée + entrée de catalogue non sélectionnable."""
    s = _cli(world["su"]).get(f"/api/v1/rag-spaces/{historique.id}/llm-settings/", **H)
    assert s.status_code == 200 and s.data["llm_model"] == "gpt-oss:20b"
    cat = _cli(world["su"]).get("/api/v1/ai/model-catalog/", **H).data
    e = {x["id"]: x for x in cat["chat"]}["gpt-oss:20b"]
    assert e["allowed"] is False and e["reason"] == mc.ERR_GENERATION_UNSUPPORTED


# ---------------------------------------------------------------------------
# Non-superuser : défaut canonique
# ---------------------------------------------------------------------------

def test_non_superuser_cree_avec_le_defaut(world, gw):
    r = _create(world["member"], world["ws"], {"name": "M1"})
    assert r.status_code == 201, r.data
    assert r.data["llm_model"] == "qwen3:30b-instruct"


def test_non_superuser_defaut_prouve_inutilisable_refuse(world, gw, settings):
    settings.DEFAULT_RAGSPACE_CHAT_MODEL = "gpt-oss:20b"
    r = _create(world["member"], world["ws"], {"name": "M2"})
    assert r.status_code == 400 and r.data["code"] == mc.ERR_GENERATION_UNSUPPORTED
    assert not RagSpace.objects.filter(name="M2").exists()


def test_non_superuser_passerelle_injoignable_conserve_le_comportement(world, gw):
    """Pas de preuve d'incompatibilité => pas de refus (comportement antérieur)."""
    gw["models"] = None
    r = _create(world["member"], world["ws"], {"name": "M3"})
    assert r.status_code == 201, r.data
