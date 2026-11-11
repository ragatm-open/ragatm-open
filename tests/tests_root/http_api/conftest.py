"""Fixtures pour les tests d'API HTTP (DRF).

IMPORTANT : APPEND_SLASH=False côté backend -> toutes les URLs doivent finir par '/'.
"""
import pytest
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace


@pytest.fixture
def admin_membership(db, workspace, user):
    """Promeut `user` owner du `workspace` (remplace un éventuel member)."""
    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Membership.ROLE_OWNER}
    )
    return user


@pytest.fixture
def admin_client(db, user, workspace, admin_membership):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def su_client(db, user, workspace, admin_membership):
    """Client SUPERUSER actif (et owner du workspace).

    Requis pour la **configuration des modèles** d'un RagSpace : depuis le verrou
    `ragspace_model_configuration_forbidden`, seul un superuser actif peut changer
    provider/modèle (chat) ou embeddings. Cf. api/v1/ragspaces/views.py et le contrat
    de référence tests/.../test_ragspace_model_policy.py.
    """
    user.is_superuser = True
    user.is_staff = True
    user.save(update_fields=["is_superuser", "is_staff"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def viewer_setup(db):
    """Un user 'viewer' sur son propre workspace + ragspace."""
    from django.contrib.auth import get_user_model

    U = get_user_model()
    v = U.objects.create_user(username="viewer", password="pw", email="viewer@e.com")
    ws = Workspace.objects.create(name="WViewer")
    Membership.objects.create(workspace=ws, user=v, role=Membership.ROLE_VIEWER)
    rs = RagSpace.objects.create(name="vs", workspace=ws)
    client = APIClient()
    client.force_authenticate(user=v)
    return {"user": v, "workspace": ws, "rag_space": rs, "client": client}


@pytest.fixture
def foreign_setup(db, other_user):
    """Un second user/workspace/ragspace totalement disjoint (pour tester l'isolation)."""
    ws = Workspace.objects.create(name="WForeign")
    Membership.objects.create(workspace=ws, user=other_user, role=Membership.ROLE_OWNER)
    rs = RagSpace.objects.create(name="foreign", workspace=ws)
    client = APIClient()
    client.force_authenticate(user=other_user)
    return {"user": other_user, "workspace": ws, "rag_space": rs, "client": client}


@pytest.fixture
def superuser(db):
    """Superutilisateur « root ». Fixture commune aux tests d'administration de la
    configuration (cinq copies locales identiques factorisées ici)."""
    from django.contrib.auth import get_user_model

    U = get_user_model()
    return U.objects.create_superuser(username="root", password="pw", email="root@e.com")


@pytest.fixture
def superuser_client(db, superuser):
    c = APIClient()
    c.force_authenticate(user=superuser)
    return c
