"""Parité du modèle par défaut entre TOUS les parcours de création d'un RagSpace.

## Le défaut corrigé

En production le 27/07/2026, `DEFAULT_RAGSPACE_CHAT_MODEL` et `AI_GATEWAY_MODEL`
valaient tous deux `qwen3:30b-instruct`, et pourtant `rag_query_log.llm_model`
portait `llama3.1:8b`. Deux causes cumulées :

1. le formulaire frontend préremplissait un littéral `llama3.1:8b` et l'ENVOYAIT,
   ce qui en faisait un « choix utilisateur » que le backend appliquait fidèlement ;
2. côté backend, un superuser qui n'envoyait aucun modèle retombait sur le défaut
   du CHAMP Django (`llama3.1:8b`), tandis qu'un non-superuser recevait
   `canonical_model_config()` — deux résultats pour la même absence de choix.

Ces tests verrouillent la convergence. Ils n'affirment JAMAIS un nom de modèle
littéral comme étant « le défaut » : ils affirment que le résultat SUIT la
configuration. Un test qui codait en dur `llama3.1:8b` était précisément ce qui
rendait la divergence invisible.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from apps.llm_router.model_defaults import ragspace_model_defaults
from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db
H = {"HTTP_HOST": "localhost"}

# Deux configurations d'environnement CONTRASTÉES. Ni l'une ni l'autre n'est « la
# bonne » : le test prouve que le résultat suit la configuration, quelle qu'elle soit.
ENV_A = {
    "DEFAULT_RAGSPACE_CHAT_PROVIDER": "ollama",
    "DEFAULT_RAGSPACE_CHAT_MODEL": "llama3.1:8b",
    "DEFAULT_RAGSPACE_EMBEDDING_PROVIDER": "ollama",
    "DEFAULT_RAGSPACE_EMBEDDING_MODEL": "nomic-embed-text",
}
ENV_B = {
    "DEFAULT_RAGSPACE_CHAT_PROVIDER": "ollama",
    "DEFAULT_RAGSPACE_CHAT_MODEL": "qwen3:30b-instruct",
    "DEFAULT_RAGSPACE_EMBEDDING_PROVIDER": "ollama",
    "DEFAULT_RAGSPACE_EMBEDDING_MODEL": "nomic-embed-text",
}
ENVS = [pytest.param(ENV_A, id="env=llama3.1:8b"), pytest.param(ENV_B, id="env=qwen3:30b-instruct")]


def _cli(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def world(db):
    ws = Workspace.objects.create(name="DEFAULTS-WS")
    owner = User.objects.create_user("dp_owner", "dp_owner@t.local", "x")
    su = User.objects.create_user(
        "dp_su", "dp_su@t.local", "x", is_superuser=True, is_staff=True
    )
    Membership.objects.create(workspace=ws, user=owner, role="owner")
    Membership.objects.create(workspace=ws, user=su, role="admin")
    return {"ws": ws, "owner": owner, "su": su}


def _url(ws):
    return f"/api/v1/workspaces/{ws.id}/ragspaces/"


# ===========================================================================
# 1. Création SANS valeur explicite — les deux parcours convergent
# ===========================================================================
@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("actor", ["su", "owner"])
def test_creation_without_explicit_models_follows_configuration(world, env, actor):
    """LE test central : superuser et utilisateur standard, même résultat.

    C'est la régression du §6 du lot : le superuser retombait sur le défaut du champ
    Django, pas sur la configuration de l'environnement.
    """
    with override_settings(**env):
        r = _cli(world[actor]).post(_url(world["ws"]), {"name": f"A-{actor}"}, format="json", **H)
        assert r.status_code == 201, r.data
        assert r.data["llm_provider"] == env["DEFAULT_RAGSPACE_CHAT_PROVIDER"]
        assert r.data["llm_model"] == env["DEFAULT_RAGSPACE_CHAT_MODEL"]
        assert r.data["embedding_provider"] == env["DEFAULT_RAGSPACE_EMBEDDING_PROVIDER"]
        assert r.data["embedding_model"] == env["DEFAULT_RAGSPACE_EMBEDDING_MODEL"]

        # La valeur ANNONCÉE par l'API est celle PERSISTÉE (pas un simple affichage).
        rs = RagSpace.objects.get(id=r.data["id"])
        assert rs.llm_model == env["DEFAULT_RAGSPACE_CHAT_MODEL"]
        assert rs.embedding_model == env["DEFAULT_RAGSPACE_EMBEDDING_MODEL"]


@pytest.mark.parametrize("env", ENVS)
def test_superuser_and_standard_user_get_the_exact_same_configuration(world, env):
    """Comparaison DIRECTE des deux parcours : aucune divergence tolérée."""
    with override_settings(**env):
        a = _cli(world["su"]).post(_url(world["ws"]), {"name": "PAR-SU"}, format="json", **H)
        b = _cli(world["owner"]).post(_url(world["ws"]), {"name": "PAR-OWN"}, format="json", **H)
    assert a.status_code == b.status_code == 201
    fields = ("llm_provider", "llm_model", "embedding_provider", "embedding_model")
    assert {f: a.data[f] for f in fields} == {f: b.data[f] for f in fields}


@pytest.mark.parametrize("env", ENVS)
def test_no_hidden_literal_survives_a_configuration_change(world, env):
    """Aucune valeur ne doit apparaître en dehors de celles configurées.

    Formulé en négatif exprès : c'est ce qu'un test « == llama3.1:8b » ne dit pas.
    """
    other = ENV_B if env is ENV_A else ENV_A
    with override_settings(**env):
        r = _cli(world["su"]).post(_url(world["ws"]), {"name": "NOLIT"}, format="json", **H)
    assert r.data["llm_model"] == env["DEFAULT_RAGSPACE_CHAT_MODEL"]
    assert r.data["llm_model"] != other["DEFAULT_RAGSPACE_CHAT_MODEL"]


# ===========================================================================
# 2. Création par ORM DIRECT (bootstrap, seeds, code futur)
# ===========================================================================
@pytest.mark.parametrize("env", ENVS)
def test_direct_orm_creation_also_follows_configuration(world, env):
    """`RagSpace.objects.create(name=...)` sans champ modèle.

    Couvre `api/v1/bootstrap/views.py` et `manage.py bootstrap_workspace`, qui
    créent l'espace initial sans énoncer de modèle. C'est par ce chemin que
    l'espace « Initial RAG » de production avait reçu `llama3.1:8b`.
    """
    with override_settings(**env):
        rs = RagSpace.objects.create(workspace=world["ws"], name="ORM")
        assert rs.llm_provider == env["DEFAULT_RAGSPACE_CHAT_PROVIDER"]
        assert rs.llm_model == env["DEFAULT_RAGSPACE_CHAT_MODEL"]
        assert rs.embedding_provider == env["DEFAULT_RAGSPACE_EMBEDDING_PROVIDER"]
        assert rs.embedding_model == env["DEFAULT_RAGSPACE_EMBEDDING_MODEL"]


def test_the_django_field_default_is_not_a_literal():
    """Garde structurelle : le défaut du champ doit être un CALLABLE.

    Un littéral y survivrait à tout changement de configuration sans que rien ne le
    signale — c'est l'origine du défaut corrigé par ce lot.
    """
    for field_name in ("llm_provider", "llm_model", "embedding_provider", "embedding_model"):
        field = RagSpace._meta.get_field(field_name)
        assert callable(field.default), (
            f"RagSpace.{field_name}.default doit être un callable lisant les settings, "
            f"trouvé le littéral {field.default!r}"
        )


# ===========================================================================
# 3. Choix explicite PRÉSERVÉ
# ===========================================================================
def test_an_explicit_choice_is_persisted_as_is(world):
    """Le correctif ne doit PAS imposer le défaut à tout le monde."""
    with override_settings(**ENV_B):
        r = _cli(world["su"]).post(
            _url(world["ws"]),
            {"name": "EXPL", "llm_provider": "ollama", "llm_model": "gemma3:27b"},
            format="json", **H,
        )
    assert r.status_code == 201, r.data
    assert r.data["llm_model"] == "gemma3:27b"
    assert RagSpace.objects.get(id=r.data["id"]).llm_model == "gemma3:27b"


def test_an_explicit_choice_equal_to_the_previous_default_is_still_honoured(world):
    """`llama3.1:8b` demandé EXPLICITEMENT reste `llama3.1:8b`, même si le défaut
    de l'environnement est autre. Un choix est un choix."""
    with override_settings(**ENV_B):
        r = _cli(world["su"]).post(
            _url(world["ws"]),
            {"name": "OLDMODEL", "llm_model": "llama3.1:8b"},
            format="json", **H,
        )
    assert r.status_code == 201, r.data
    assert r.data["llm_model"] == "llama3.1:8b"


def test_a_partial_payload_completes_only_the_missing_fields(world):
    """Le chat est choisi, les embeddings sont omis -> seuls ces derniers sont complétés."""
    with override_settings(**ENV_B):
        r = _cli(world["su"]).post(
            _url(world["ws"]),
            {"name": "PARTIAL", "llm_model": "gemma3:27b"},
            format="json", **H,
        )
    assert r.status_code == 201, r.data
    assert r.data["llm_model"] == "gemma3:27b"
    assert r.data["embedding_model"] == ENV_B["DEFAULT_RAGSPACE_EMBEDDING_MODEL"]


# ===========================================================================
# 4. Aucun espace EXISTANT n'est modifié
# ===========================================================================
@pytest.mark.parametrize("env", ENVS)
def test_existing_spaces_keep_their_persisted_model(world, env):
    """Un espace historique conserve sa valeur, même si le défaut change.

    Le défaut ne s'applique qu'à la CRÉATION : il n'existe aucune réécriture
    rétroactive, ni au démarrage, ni par migration.
    """
    with override_settings(**ENV_A):
        historic = RagSpace.objects.create(
            workspace=world["ws"], name="HIST", llm_model="llama3.1:8b"
        )
    with override_settings(**env):
        # Une création dans un autre environnement ne touche pas l'existant.
        _cli(world["su"]).post(_url(world["ws"]), {"name": "NEW"}, format="json", **H)
        historic.refresh_from_db()
        assert historic.llm_model == "llama3.1:8b"


def test_creating_a_space_never_marks_documents_for_reindexing(world):
    """Aucune réindexation n'est déclenchée par ce lot.

    Le marquage `reindex_required` n'existe que sur le chemin PATCH, quand les
    EMBEDDINGS changent. La création n'y touche pas, et les défauts d'embeddings
    sont inchangés (`nomic-embed-text`).
    """
    from apps.rag_core.models import Document

    with override_settings(**ENV_A):
        space = RagSpace.objects.create(workspace=world["ws"], name="WITHDOCS")
        doc = Document.objects.create(rag_space=space, filename="a.txt", status="READY")
    assert doc.reindex_required is False

    with override_settings(**ENV_B):
        _cli(world["su"]).post(_url(world["ws"]), {"name": "OTHER"}, format="json", **H)

    doc.refresh_from_db()
    assert doc.reindex_required is False, (
        "la création d'un espace ne doit jamais marquer un document à réindexer"
    )


# ===========================================================================
# 5. Source de vérité UNIQUE : l'API expose exactement ce qu'elle persiste
# ===========================================================================
@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("actor", ["su", "owner"])
def test_capabilities_endpoint_announces_the_values_actually_used(world, env, actor):
    """Le frontend s'initialise depuis `/capabilities/` : ce qu'il affiche doit être
    exactement ce que la création persistera. Sinon l'écran mentirait."""
    with override_settings(**env):
        caps = _cli(world[actor]).get("/api/v1/capabilities/", **H)
        assert caps.status_code == 200
        policy = caps.data["ragspace_model_policy"]

        created = _cli(world[actor]).post(
            _url(world["ws"]), {"name": f"SYNC-{actor}"}, format="json", **H
        )
    assert created.status_code == 201, created.data
    for field in ("llm_provider", "llm_model", "embedding_provider", "embedding_model"):
        assert policy[field] == created.data[field], (
            f"{field} : l'écran annoncerait {policy[field]!r} et la base porterait "
            f"{created.data[field]!r}"
        )


@pytest.mark.parametrize("env", ENVS)
def test_the_defaults_helper_matches_the_settings(env):
    """`ragspace_model_defaults()` relit les settings à chaque appel (pas de cache
    figé à l'import, sinon `override_settings` et un redéploiement seraient sans effet)."""
    with override_settings(**env):
        assert ragspace_model_defaults() == {
            "llm_provider": env["DEFAULT_RAGSPACE_CHAT_PROVIDER"],
            "llm_model": env["DEFAULT_RAGSPACE_CHAT_MODEL"],
            "embedding_provider": env["DEFAULT_RAGSPACE_EMBEDDING_PROVIDER"],
            "embedding_model": env["DEFAULT_RAGSPACE_EMBEDDING_MODEL"],
        }
