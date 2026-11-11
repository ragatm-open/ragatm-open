"""Fixtures partagées des tests « corpus métier ».

Le propriétaire global ragATM est mappé sur ``is_superuser`` (cf.
apps.accounts.permissions). On fournit ici un utilisateur global, des clients
DRF authentifiés pour chaque rôle, et des fabriques de domaine/corpus.
"""
import pytest
from rest_framework.test import APIClient

from django.contrib.auth import get_user_model

from apps.corpus.models import BusinessDomain, DomainCorpus, WorkspaceDomainCorpusAccess
from apps.workspaces.models import Membership, Workspace


@pytest.fixture
def global_owner(db):
    """Propriétaire global ragATM (superuser), DISTINCT de tout rôle workspace."""
    User = get_user_model()
    return User.objects.create_user(
        username="ragatm_owner",
        password="pw",
        email="owner@ragatm.test",
        is_staff=True,
        is_superuser=True,
    )


@pytest.fixture
def global_owner_client(db, global_owner):
    client = APIClient()
    client.force_authenticate(user=global_owner)
    return client


@pytest.fixture
def ws_admin(db):
    """Admin d'un workspace — NE doit jamais pouvoir administrer les corpus."""
    User = get_user_model()
    u = User.objects.create_user(username="ws_admin", password="pw", email="admin@e.com")
    ws = Workspace.objects.create(name="WAdmin")
    Membership.objects.create(workspace=ws, user=u, role=Membership.ROLE_ADMIN)
    client = APIClient()
    client.force_authenticate(user=u)
    return {"user": u, "workspace": ws, "client": client}


@pytest.fixture
def viewer_setup(db):
    """Un user 'viewer' sur son propre workspace."""
    User = get_user_model()
    v = User.objects.create_user(username="viewer_c", password="pw", email="viewer_c@e.com")
    ws = Workspace.objects.create(name="WViewerC")
    Membership.objects.create(workspace=ws, user=v, role=Membership.ROLE_VIEWER)
    client = APIClient()
    client.force_authenticate(user=v)
    return {"user": v, "workspace": ws, "client": client}


@pytest.fixture
def foreign_setup(db, other_user):
    """User/workspace disjoint, pour tester l'isolation (non-membre de `workspace`)."""
    ws = Workspace.objects.create(name="WForeignC")
    Membership.objects.create(workspace=ws, user=other_user, role=Membership.ROLE_OWNER)
    client = APIClient()
    client.force_authenticate(user=other_user)
    return {"user": other_user, "workspace": ws, "client": client}


@pytest.fixture
def business_domain(db):
    return BusinessDomain.objects.create(slug="logistique", name="Logistique")


@pytest.fixture
def domain_corpus(db, business_domain):
    return DomainCorpus.objects.create(
        domain=business_domain,
        slug="qualite",
        name="Procédures qualité",
        embedding_provider="ai_gateway",
        embedding_model="legacy-768",
    )


@pytest.fixture
def grant_access():
    """Associe (enabled) un corpus à un workspace."""

    def _grant(*, workspace, corpus, enabled=True):
        access, _ = WorkspaceDomainCorpusAccess.objects.update_or_create(
            workspace=workspace, corpus=corpus, defaults={"enabled": enabled}
        )
        return access

    return _grant
