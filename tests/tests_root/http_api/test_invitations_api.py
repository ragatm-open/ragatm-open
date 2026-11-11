"""Tests HTTP du domaine d'invitation (INV-A2/A3/A4).

Permissions, one-shot URL, no-store, liste sans secret, acceptation
(nouveau/actif), régénération, révocation, confidentialité.
"""
import pytest
from rest_framework.test import APIClient

from apps.workspaces.models import Membership, Workspace, WorkspaceInvitation

STRONG_PW = "Corr3ct-Horse-Staple-88"


def _mk_user(username, email, role, workspace):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    u = U.objects.create_user(username=username, email=email, password="pw-really-strong-1")
    Membership.objects.create(workspace=workspace, user=u, role=role)
    c = APIClient()
    c.force_authenticate(user=u)
    return u, c


@pytest.fixture
def setup(db):
    ws = Workspace.objects.create(name="WInv")
    owner, owner_c = _mk_user("o", "o@ex.com", Membership.ROLE_OWNER, ws)
    admin, admin_c = _mk_user("a", "a@ex.com", Membership.ROLE_ADMIN, ws)
    member, member_c = _mk_user("m", "m@ex.com", Membership.ROLE_MEMBER, ws)
    viewer, viewer_c = _mk_user("v", "v@ex.com", Membership.ROLE_VIEWER, ws)
    # workspace étranger
    ws2 = Workspace.objects.create(name="WOther")
    fadmin, fadmin_c = _mk_user("fa", "fa@ex.com", Membership.ROLE_ADMIN, ws2)
    return locals()


def _create(client, ws_id, email="invitee@ex.com", role="viewer"):
    return client.post(
        f"/api/v1/workspaces/{ws_id}/invitations/",
        {"email": email, "role": role},
        format="json",
    )


# ----------------------------------------------------------------- permissions
@pytest.mark.django_db
def test_owner_and_admin_can_invite(setup):
    assert _create(setup["owner_c"], setup["ws"].id, email="i1@ex.com").status_code == 201
    assert _create(setup["admin_c"], setup["ws"].id, email="i2@ex.com").status_code == 201


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["member_c", "viewer_c"])
def test_member_and_viewer_cannot_invite(setup, who):
    assert _create(setup[who], setup["ws"].id).status_code == 403


@pytest.mark.django_db
def test_non_member_cannot_invite(setup):
    c = APIClient()  # anonyme
    assert _create(c, setup["ws"].id).status_code in (401, 403)


@pytest.mark.django_db
def test_cross_workspace_admin_cannot_invite(setup):
    # admin du ws2 tente d'inviter dans ws1
    assert _create(setup["fadmin_c"], setup["ws"].id).status_code == 403


# ----------------------------------------------------------------- création
@pytest.mark.django_db
def test_creation_returns_one_shot_url_and_no_store(setup):
    resp = _create(setup["owner_c"], setup["ws"].id, email="one@ex.com")
    assert resp.status_code == 201
    body = resp.json()
    assert "invitation_url" in body and "/accept-invitation?token=" in body["invitation_url"]
    assert resp["Cache-Control"] == "no-store"
    assert resp["Pragma"] == "no-cache"


@pytest.mark.django_db
def test_owner_role_rejected_on_create(setup):
    resp = _create(setup["owner_c"], setup["ws"].id, email="own@ex.com", role="owner")
    assert resp.status_code == 400  # serializer choice invalide


@pytest.mark.django_db
def test_duplicate_pending_is_conflict(setup):
    _create(setup["owner_c"], setup["ws"].id, email="dup@ex.com")
    resp = _create(setup["owner_c"], setup["ws"].id, email="dup@ex.com")
    assert resp.status_code == 409


# ----------------------------------------------------------------- liste
@pytest.mark.django_db
def test_list_never_returns_url_or_token(setup):
    _create(setup["owner_c"], setup["ws"].id, email="list@ex.com")
    resp = setup["owner_c"].get(f"/api/v1/workspaces/{setup['ws'].id}/invitations/")
    assert resp.status_code == 200
    raw = resp.content.decode()
    assert "invitation_url" not in raw
    assert "token" not in raw
    item = resp.json()[0]
    assert set(item) == {
        "id", "email", "role", "status", "generation",
        "created_at", "expires_at", "can_regenerate", "can_revoke",
    }
    assert item["status"] == "pending"


# ----------------------------------------------------------------- acceptation
def _extract_token(create_resp):
    return create_resp.json()["invitation_url"].split("token=", 1)[1]


@pytest.mark.django_db
def test_new_user_accept_creates_membership(setup):
    resp = _create(setup["owner_c"], setup["ws"].id, email="fresh@ex.com", role="member")
    token = _extract_token(resp)
    ws_id = setup["ws"].id
    # Aucun membership avant acceptation
    from django.contrib.auth import get_user_model

    U = get_user_model()
    assert not Membership.objects.filter(workspace_id=ws_id, user__email__iexact="fresh@ex.com").exists()

    c = APIClient()
    a = c.post(
        "/api/v1/invitations/accept/",
        {"token": token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    assert a.status_code == 200, a.content
    u = U.objects.get(email__iexact="fresh@ex.com")
    assert u.is_active and u.check_password(STRONG_PW)
    assert Membership.objects.get(workspace_id=ws_id, user=u).role == "member"


@pytest.mark.django_db
def test_active_user_must_authenticate(setup):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    U.objects.create_user(username="act", email="act@ex.com", password="ExistingPwd-77")
    token = _extract_token(_create(setup["owner_c"], setup["ws"].id, email="act@ex.com"))
    c = APIClient()
    a = c.post(
        "/api/v1/invitations/accept/",
        {"token": token, "password": "NewPwd-Attacker-9", "password_confirmation": "NewPwd-Attacker-9"},
        format="json",
    )
    assert a.status_code == 409
    assert a.json()["code"] == "active_user_must_authenticate"
    U.objects.get(email="act@ex.com").check_password("ExistingPwd-77")


@pytest.mark.django_db
def test_active_user_invitation_never_resets_password(setup):
    """Preuve INV-A0/A2 : un lien d'invitation ne modifie jamais le mot de passe
    d'un compte actif, et ne crée pas d'accès sans authentification."""
    from django.contrib.auth import get_user_model

    U = get_user_model()
    active = U.objects.create_user(username="prot", email="prot@ex.com", password="ExistingPwd-77")
    token = _extract_token(_create(setup["owner_c"], setup["ws"].id, email="prot@ex.com"))
    a = APIClient().post(
        "/api/v1/invitations/accept/",
        {"token": token, "password": "Hijack-Pwd-2026", "password_confirmation": "Hijack-Pwd-2026"},
        format="json",
    )
    assert a.status_code == 409 and a.json()["code"] == "active_user_must_authenticate"
    active.refresh_from_db()
    assert active.check_password("ExistingPwd-77")  # mot de passe INCHANGÉ
    assert not Membership.objects.filter(workspace_id=setup["ws"].id, user=active).exists()


@pytest.mark.django_db
def test_authenticated_accept_email_must_match(setup):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    other = U.objects.create_user(username="wrong", email="wrong@ex.com", password="ExistingPwd-77")
    token = _extract_token(_create(setup["owner_c"], setup["ws"].id, email="target@ex.com"))
    c = APIClient()
    c.force_authenticate(user=other)
    a = c.post("/api/v1/invitations/accept-authenticated/", {"token": token}, format="json")
    assert a.status_code == 403
    assert a.json()["code"] == "email_mismatch"


@pytest.mark.django_db
def test_active_user_authenticated_accept_ok(setup):
    from django.contrib.auth import get_user_model

    U = get_user_model()
    active = U.objects.create_user(username="ok", email="ok@ex.com", password="ExistingPwd-77")
    token = _extract_token(_create(setup["owner_c"], setup["ws"].id, email="ok@ex.com", role="admin"))
    c = APIClient()
    c.force_authenticate(user=active)
    a = c.post("/api/v1/invitations/accept-authenticated/", {"token": token}, format="json")
    assert a.status_code == 200
    assert Membership.objects.get(workspace_id=setup["ws"].id, user=active).role == "admin"
    active.refresh_from_db()
    assert active.check_password("ExistingPwd-77")  # inchangé


# ----------------------------------------------------------------- validate
@pytest.mark.django_db
def test_validate_endpoint_reports_state(setup):
    token = _extract_token(_create(setup["owner_c"], setup["ws"].id, email="val@ex.com"))
    c = APIClient()
    r = c.post("/api/v1/invitations/validate/", {"token": token}, format="json")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["account_active"] is False
    assert body["workspace_name"] == "WInv"
    assert "@" in body["email_masked"] and "val@ex.com" not in body["email_masked"]


# ----------------------------------------------------------------- régénération
@pytest.mark.django_db
def test_regeneration_old_invalid_new_valid(setup):
    resp = _create(setup["owner_c"], setup["ws"].id, email="regen@ex.com")
    old_token = _extract_token(resp)
    inv_id = resp.json()["id"]
    r = setup["owner_c"].post(
        f"/api/v1/workspaces/{setup['ws'].id}/invitations/{inv_id}/regenerate/", {}, format="json"
    )
    assert r.status_code == 200
    assert r["Cache-Control"] == "no-store"
    assert r.json()["generation"] == 2
    new_token = _extract_token(r)
    assert new_token != old_token

    c = APIClient()
    # ancien token invalide
    a_old = c.post(
        "/api/v1/invitations/accept/",
        {"token": old_token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    assert a_old.status_code == 400 and a_old.json()["code"] == "invalid_token"
    # nouveau token valide
    a_new = c.post(
        "/api/v1/invitations/accept/",
        {"token": new_token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    assert a_new.status_code == 200


@pytest.mark.django_db
def test_accepted_invitation_cannot_be_regenerated(setup):
    resp = _create(setup["owner_c"], setup["ws"].id, email="accr@ex.com")
    token = _extract_token(resp)
    inv_id = resp.json()["id"]
    APIClient().post(
        "/api/v1/invitations/accept/",
        {"token": token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    r = setup["owner_c"].post(
        f"/api/v1/workspaces/{setup['ws'].id}/invitations/{inv_id}/regenerate/", {}, format="json"
    )
    assert r.status_code == 409


# ----------------------------------------------------------------- révocation
@pytest.mark.django_db
def test_revocation_invalidates_and_is_idempotent(setup):
    resp = _create(setup["owner_c"], setup["ws"].id, email="rev@ex.com")
    token = _extract_token(resp)
    inv_id = resp.json()["id"]
    r1 = setup["owner_c"].post(
        f"/api/v1/workspaces/{setup['ws'].id}/invitations/{inv_id}/revoke/", {}, format="json"
    )
    assert r1.status_code == 200 and r1.json()["changed"] is True
    r2 = setup["owner_c"].post(
        f"/api/v1/workspaces/{setup['ws'].id}/invitations/{inv_id}/revoke/", {}, format="json"
    )
    assert r2.status_code == 200 and r2.json()["changed"] is False
    a = APIClient().post(
        "/api/v1/invitations/accept/",
        {"token": token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    assert a.status_code == 400 and a.json()["code"] == "invitation_revoked"


@pytest.mark.django_db
def test_revoke_refused_after_accept(setup):
    resp = _create(setup["owner_c"], setup["ws"].id, email="ra@ex.com")
    token = _extract_token(resp)
    inv_id = resp.json()["id"]
    APIClient().post(
        "/api/v1/invitations/accept/",
        {"token": token, "password": STRONG_PW, "password_confirmation": STRONG_PW},
        format="json",
    )
    r = setup["owner_c"].post(
        f"/api/v1/workspaces/{setup['ws'].id}/invitations/{inv_id}/revoke/", {}, format="json"
    )
    assert r.status_code == 409
