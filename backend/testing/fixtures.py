"""Fixtures canoniques partagées par les deux arbres de tests backend.

Principe : ce module ne fournit QUE les garanties réellement communes aux arbres.
Toute variante métier (appartenance à un workspace, email de connexion, rôle
particulier) reste explicite, dans une fixture spécialisée ou dans le test concerné.
Enrichir une fixture partagée par confort ferait dépendre des centaines de tests de
données qu'ils n'ont jamais demandées.
"""
import pytest
from django.contrib.auth import get_user_model


@pytest.fixture
def user(db):
    """Utilisateur actif minimal : identité et mot de passe utilisable, rien de plus.

    Contrat : `username="u1"`, mot de passe `"pw"`, `is_active=True`, AUCUNE
    appartenance, AUCUNE dépendance à `workspace` ou `rag_space`.

    Pas d'email : le champ n'est utilisé que par les tests qui se connectent
    réellement via l'endpoint de login (lequel authentifie par email). Ces tests
    demandent `user_with_email`, qui rend la dépendance visible au point d'usage.
    """
    return get_user_model().objects.create_user(username="u1", password="pw")


@pytest.fixture
def rag_space(workspace):
    """Espace documentaire minimal rattaché au `workspace` du test.

    Contrat : `name="space-a"`, réglages LLM et embeddings laissés aux DÉFAUTS du
    modèle (ollama / compact-generation-8 / profil d'embeddings par défaut), aucun `prompt_template`, non
    supprimé, aucune appartenance ni objet lié.

    `workspace` n'est volontairement PAS résolu ici : la dépendance est déclarée, et
    pytest la satisfait depuis le conftest de l'arbre appelant. Chaque test conserve
    donc exactement le workspace qu'il avait avant la convergence.

    Le nom est conventionnel : aucun test n'observe cette valeur. Le fixer permet de
    ne pas laisser croire qu'elle porte un sens métier.
    """
    from apps.rag_core.models import RagSpace

    return RagSpace.objects.create(name="space-a", workspace=workspace)


@pytest.fixture
def member(db, workspace, user):
    """Appartenance simple : le `user` du test est membre du `workspace` du test.

    Contrat : rôle `ROLE_MEMBER`, retourne l'instance `Membership`. Aucune promotion,
    aucun autre rôle, aucun effet de bord.

    Comme pour `rag_space`, `workspace` et `user` sont DÉCLARÉS sans être résolus ici :
    pytest les satisfait depuis le conftest de l'arbre appelant.

    À ne pas confondre avec `admin_membership` (arbre racine), qui PROMEUT l'utilisateur
    owner via `update_or_create` et écrase donc une appartenance existante.
    """
    from apps.workspaces.models import Membership

    return Membership.objects.create(
        workspace=workspace, user=user, role=Membership.ROLE_MEMBER
    )


@pytest.fixture
def api_client(db, user, member):
    """Client DRF authentifié comme membre du `workspace` du test.

    Contrat : `APIClient` authentifié par `force_authenticate` sur le `user` du test,
    lequel est membre (`ROLE_MEMBER`) du `workspace` via `member`. Aucune vérification
    CSRF, aucun en-tête, aucun format par défaut, aucune mutation après création.

    `member` est déclaré pour l'effet de bord : sans lui l'utilisateur serait
    authentifié mais sans appartenance, donc sans permission. La dépendance rend cette
    garantie explicite plutôt qu'accidentelle.

    Sept définitions locales réalisaient ce contrat à l'identique. L'équivalence a été
    établie à l'EXÉCUTION — mécanisme d'authentification, identité vue par /auth/me/,
    rôles, absence de mutation du client et ordre de création des objets — et non par
    lecture des corps de fixtures.
    """
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def workspace(db):
    """Espace de travail minimal : un nom, rien d'autre.

    Contrat : `name="W1"`, AUCUN propriétaire (le modèle n'a pas ce champ — les
    permissions passent par `Membership`), AUCUNE appartenance, AUCUN objet lié.

    Le nom est conventionnel : aucun test ne l'observe. Les quatre définitions locales
    produisaient un contrat identique à l'exécution — mêmes champs, zéro appartenance,
    zéro RagSpace lié, même ordre de création.
    """
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="W1")
