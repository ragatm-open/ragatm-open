"""Tests unitaires du service d'invitation (INV-A1/A2/A3) — niveau service.

Couvre : token haché uniquement, one-pending, owner interdit, acceptation
nouveau/actif, rôle issu de la DB, régénération invalidant l'ancien token,
révocation invalidant le token, expiration.
"""
import hashlib

import pytest
from django.utils import timezone

from apps.workspaces.models import Membership, Workspace, WorkspaceInvitation
from apps.workspaces.services import invitations as inv


@pytest.fixture
def ws(db):
    return Workspace.objects.create(name="WSvc")


@pytest.mark.django_db
def test_token_hash_only_stored(ws):
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="A@Ex.com", role="viewer")
    assert invitation.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert len(invitation.token_hash) == 64
    # Le brut n'est stocké nulle part sur l'objet.
    assert raw not in (invitation.token_hash,)
    assert invitation.email_normalized == "a@ex.com"  # normalisation minuscule


@pytest.mark.django_db
def test_token_is_url_safe_and_high_entropy(ws):
    _, raw = inv.create_invitation(workspace_id=ws.id, email="b@ex.com", role="viewer")
    import re

    assert re.fullmatch(r"[A-Za-z0-9_-]+", raw)
    assert len(raw) >= 40  # token_urlsafe(32) ≈ 43 chars


@pytest.mark.django_db
def test_owner_role_forbidden(ws):
    with pytest.raises(inv.InvitationRoleForbidden):
        inv.create_invitation(workspace_id=ws.id, email="c@ex.com", role="owner")


@pytest.mark.django_db
def test_one_pending_invitation_per_workspace_email(ws):
    inv.create_invitation(workspace_id=ws.id, email="dup@ex.com", role="viewer")
    with pytest.raises(inv.InvitationAlreadyPending):
        inv.create_invitation(workspace_id=ws.id, email="DUP@ex.com", role="member")


@pytest.mark.django_db
def test_new_user_accept_sets_password_and_creates_membership(ws):
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="new@ex.com", role="member")
    # Aucun membership avant acceptation.
    assert not Membership.objects.filter(workspace=ws).exists()
    res = inv.accept_invitation(raw_token=raw, password="Sûr-Motdepasse-92", password_confirmation="Sûr-Motdepasse-92")
    assert res["membership_created"] is True
    assert res["membership"].role == "member"
    u = res["user"]
    assert u.is_active and u.check_password("Sûr-Motdepasse-92")
    invitation.refresh_from_db()
    assert invitation.accepted_at is not None
    assert invitation.status() == WorkspaceInvitation.STATUS_ACCEPTED


@pytest.mark.django_db
def test_active_user_anonymous_accept_is_refused(ws):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    active = U.objects.create_user(username="act", email="act@ex.com", password="ExistingPwd-77")
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="act@ex.com", role="viewer")
    with pytest.raises(inv.ActiveUserMustAuthenticate):
        inv.accept_invitation(raw_token=raw, password="NewPwd-Attacker-9", password_confirmation="NewPwd-Attacker-9")
    active.refresh_from_db()
    assert active.check_password("ExistingPwd-77")  # mot de passe inchangé
    invitation.refresh_from_db()
    assert invitation.accepted_at is None


@pytest.mark.django_db
def test_active_user_authenticated_accept_never_changes_password(ws):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    active = U.objects.create_user(username="act2", email="act2@ex.com", password="ExistingPwd-77")
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="act2@ex.com", role="admin")
    res = inv.accept_invitation(raw_token=raw, authenticated_user=active)
    assert res["membership"].role == "admin"
    active.refresh_from_db()
    assert active.check_password("ExistingPwd-77")


@pytest.mark.django_db
def test_authenticated_accept_email_must_match(ws):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    other = U.objects.create_user(username="other", email="other@ex.com", password="pw-really-strong-1")
    _, raw = inv.create_invitation(workspace_id=ws.id, email="target@ex.com", role="viewer")
    with pytest.raises(inv.EmailMismatch):
        inv.accept_invitation(raw_token=raw, authenticated_user=other)


@pytest.mark.django_db
def test_regeneration_invalidates_old_token(ws):
    invitation, old_raw = inv.create_invitation(workspace_id=ws.id, email="regen@ex.com", role="viewer")
    invitation2, new_raw = inv.regenerate_invitation(workspace_id=ws.id, invitation_id=invitation.id)
    assert invitation2.generation == 2
    assert new_raw != old_raw
    # Ancien token rejeté, nouveau accepté.
    with pytest.raises(inv.InvitationInvalidToken):
        inv.accept_invitation(raw_token=old_raw, password="Zzz-Strong-Pw-42", password_confirmation="Zzz-Strong-Pw-42")
    res = inv.accept_invitation(raw_token=new_raw, password="Zzz-Strong-Pw-42", password_confirmation="Zzz-Strong-Pw-42")
    assert res["membership_created"] is True


@pytest.mark.django_db
def test_revocation_invalidates_token_and_is_idempotent(ws):
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="rev@ex.com", role="viewer")
    _, changed = inv.revoke_invitation(workspace_id=ws.id, invitation_id=invitation.id)
    assert changed is True
    _, changed2 = inv.revoke_invitation(workspace_id=ws.id, invitation_id=invitation.id)
    assert changed2 is False  # idempotent
    with pytest.raises(inv.InvitationRevoked):
        inv.accept_invitation(raw_token=raw, password="Zzz-Strong-Pw-42", password_confirmation="Zzz-Strong-Pw-42")


@pytest.mark.django_db
def test_accepted_invitation_cannot_be_regenerated_or_revoked(ws):
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="acc@ex.com", role="viewer")
    inv.accept_invitation(raw_token=raw, password="Zzz-Strong-Pw-42", password_confirmation="Zzz-Strong-Pw-42")
    with pytest.raises(inv.InvitationAlreadyAccepted):
        inv.regenerate_invitation(workspace_id=ws.id, invitation_id=invitation.id)
    with pytest.raises(inv.InvitationAlreadyAccepted):
        inv.revoke_invitation(workspace_id=ws.id, invitation_id=invitation.id)


@pytest.mark.django_db
def test_expired_token_rejected(ws):
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="exp@ex.com", role="viewer")
    WorkspaceInvitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - timezone.timedelta(minutes=1)
    )
    with pytest.raises(inv.InvitationExpired):
        inv.accept_invitation(raw_token=raw, password="Zzz-Strong-Pw-42", password_confirmation="Zzz-Strong-Pw-42")


@pytest.mark.django_db
def test_role_comes_from_invitation_not_client(ws):
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="role@ex.com", role="viewer")
    # Aucun paramètre de rôle n'est accepté par accept_invitation → rôle = DB.
    res = inv.accept_invitation(raw_token=raw, password="Zzz-Strong-Pw-42", password_confirmation="Zzz-Strong-Pw-42")
    assert res["membership"].role == "viewer"


@pytest.mark.django_db
def test_url_uses_frontend_origin(ws, settings):
    settings.FRONTEND_APP_ORIGIN = "https://app.example.com"
    _, raw = inv.create_invitation(workspace_id=ws.id, email="url@ex.com", role="viewer")
    url = inv.build_invitation_url(raw)
    assert url == f"https://app.example.com/accept-invitation?token={raw}"
