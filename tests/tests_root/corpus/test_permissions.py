"""Phase 1 — Rôle propriétaire global ragATM.

Le propriétaire global est STRICTEMENT distinct des rôles de workspace. On teste
ici la brique de base ``is_ragatm_owner`` / ``IsRagATMOwner`` indépendamment de
tout endpoint (les permissions au niveau API sont re-testées en Phase 3).
"""
import pytest
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from apps.accounts.permissions import IsRagATMOwner, is_ragatm_owner
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db


def _has_perm(user) -> bool:
    factory = APIRequestFactory()
    request = factory.get("/x/")
    request.user = user
    return IsRagATMOwner().has_permission(request, APIView())


def test_superuser_is_global_owner(global_owner):
    assert is_ragatm_owner(global_owner) is True
    assert _has_perm(global_owner) is True


def test_workspace_admin_is_not_global_owner(ws_admin):
    user = ws_admin["user"]
    assert is_ragatm_owner(user) is False
    assert _has_perm(user) is False


def test_workspace_owner_is_not_global_owner(db):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    u = U.objects.create_user(username="wsowner", password="pw", email="wso@e.com")
    ws = Workspace.objects.create(name="WOwner")
    Membership.objects.create(workspace=ws, user=u, role=Membership.ROLE_OWNER)
    assert is_ragatm_owner(u) is False
    assert _has_perm(u) is False


def test_member_and_viewer_are_not_global_owner(db):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    ws = Workspace.objects.create(name="WRoles")
    for role in (Membership.ROLE_MEMBER, Membership.ROLE_VIEWER):
        u = U.objects.create_user(username=f"u_{role}", password="pw", email=f"{role}@e.com")
        Membership.objects.create(workspace=ws, user=u, role=role)
        assert is_ragatm_owner(u) is False
        assert _has_perm(u) is False


def test_anonymous_is_not_global_owner(db):
    from django.contrib.auth.models import AnonymousUser

    assert is_ragatm_owner(AnonymousUser()) is False
    assert _has_perm(AnonymousUser()) is False


def test_inactive_superuser_is_not_global_owner(db):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    u = U.objects.create_user(
        username="inactive_su", password="pw", email="i@e.com",
        is_superuser=True, is_active=False,
    )
    assert is_ragatm_owner(u) is False
