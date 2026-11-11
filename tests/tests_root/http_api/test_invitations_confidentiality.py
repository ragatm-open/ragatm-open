"""Confidentialité du token d'invitation (INV-A4/A7).

Le token brut ne doit apparaître ni en base (hors hash), ni dans l'audit, ni
dans les logs, ni dans les réponses de liste/erreur.
"""
import logging

import pytest
from rest_framework.test import APIClient

from apps.audit.models import ActionAuditEvent
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import invitations as inv

STRONG_PW = "Corr3ct-Horse-Staple-88"


@pytest.fixture
def owner_client(db):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    ws = Workspace.objects.create(name="WConf")
    owner = U.objects.create_user(username="oc", email="oc@ex.com", password="pw-really-strong-1")
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    c = APIClient()
    c.force_authenticate(user=owner)
    return c, ws


@pytest.mark.django_db
def test_token_absent_from_audit(owner_client):
    c, ws = owner_client
    resp = c.post(
        f"/api/v1/workspaces/{ws.id}/invitations/",
        {"email": "aud@ex.com", "role": "viewer"},
        format="json",
    )
    token = resp.json()["invitation_url"].split("token=", 1)[1]
    # accepte pour générer aussi invitation_accepted
    APIClient().post(
        "/api/v1/invitations/accept/",
        {"token": token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    events = list(ActionAuditEvent.objects.filter(action_name="workspace_invitation"))
    assert events, "au moins un événement d'audit d'invitation attendu"
    names = {e.event_name for e in events}
    assert "invitation_created" in names and "invitation_accepted" in names
    # Le token brut n'apparaît dans aucun champ sérialisé de l'audit.
    for e in events:
        blob = f"{e.event_name}{e.metadata}{e.resource_id}{e.error_code}"
        assert token not in blob


@pytest.mark.django_db
def test_token_absent_from_logs(owner_client, caplog):
    c, ws = owner_client
    with caplog.at_level(logging.DEBUG):
        resp = c.post(
            f"/api/v1/workspaces/{ws.id}/invitations/",
            {"email": "log@ex.com", "role": "viewer"},
            format="json",
        )
        token = resp.json()["invitation_url"].split("token=", 1)[1]
        APIClient().post(
            "/api/v1/invitations/accept/",
            {"token": token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
            format="json",
        )
    assert token not in caplog.text


@pytest.mark.django_db
def test_token_absent_from_error_payload(owner_client):
    c, ws = owner_client
    # token invalide → réponse d'erreur ne renvoie jamais le token soumis
    bad = "z" * 43
    r = APIClient().post(
        "/api/v1/invitations/accept/",
        {"token": bad, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    assert r.status_code == 400
    assert bad not in r.content.decode()
    assert r["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_raw_token_never_stored(owner_client):
    c, ws = owner_client
    resp = c.post(
        f"/api/v1/workspaces/{ws.id}/invitations/",
        {"email": "store@ex.com", "role": "viewer"},
        format="json",
    )
    token = resp.json()["invitation_url"].split("token=", 1)[1]
    from apps.workspaces.models import WorkspaceInvitation

    invitation = WorkspaceInvitation.objects.get(email_normalized="store@ex.com")
    assert invitation.token_hash != token
    assert invitation.token_hash == inv.hash_token(token)
