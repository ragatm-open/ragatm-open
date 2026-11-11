"""P-A — FRONTEND_APP_ORIGIN unifié pour les liens email.

Vérifie que les liens d'invitation workspace ET de reset password utilisent
la même origine, lue depuis settings.FRONTEND_APP_ORIGIN (donc l'environnement).
Aucun domaine réel n'est utilisé : on surcharge via override_settings.
"""
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from apps.workspaces.models import Workspace, Membership

pytestmark = pytest.mark.django_db

FRONT = "https://front.example.test"


@override_settings(FRONTEND_APP_ORIGIN=FRONT, DEBUG=True)
def test_reset_password_link_uses_frontend_origin(user_with_email):
    client = APIClient()
    res = client.post(
        "/api/v1/auth/password/reset/",
        {"email": "u1@example.com"},
        format="json",
    )
    assert res.status_code == 200
    # En DEBUG, l'API renvoie debug_link pour test sans SMTP.
    assert res.data["debug_link"].startswith(f"{FRONT}/reset-password")


@override_settings(FRONTEND_APP_ORIGIN=FRONT)
def test_invite_link_uses_frontend_origin(db):
    """Domaine v2 : le lien d'invitation pointe vers {FRONT}/accept-invitation."""
    User = get_user_model()
    admin = User.objects.create_user(username="admin", email="admin@example.com", password="pw")
    ws = Workspace.objects.create(name="W")
    Membership.objects.create(workspace=ws, user=admin, role=Membership.ROLE_OWNER)

    client = APIClient()
    client.force_authenticate(user=admin)
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/invitations/",
        {"email": "newcomer@example.com", "role": "viewer"},
        format="json",
    )
    assert res.status_code == 201
    assert res.data["invitation_url"].startswith(f"{FRONT}/accept-invitation?token=")


@override_settings(FRONTEND_APP_ORIGIN=FRONT, DEBUG=True)
def test_invite_and_reset_links_share_same_origin(db):
    """Les deux flux (invitation v2 + reset password) partagent la même origine."""
    User = get_user_model()
    admin = User.objects.create_user(username="admin2", email="admin2@example.com", password="pw")
    ws = Workspace.objects.create(name="W2")
    Membership.objects.create(workspace=ws, user=admin, role=Membership.ROLE_OWNER)

    client = APIClient()
    client.force_authenticate(user=admin)
    invite = client.post(
        f"/api/v1/workspaces/{ws.id}/invitations/",
        {"email": "share@example.com", "role": "viewer"},
        format="json",
    )

    anon = APIClient()
    reset = anon.post(
        "/api/v1/auth/password/reset/",
        {"email": "admin2@example.com"},
        format="json",
    )

    assert invite.data["invitation_url"].startswith(FRONT)
    assert reset.data["debug_link"].startswith(FRONT)
