import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts.mfa import issue_auth_token
from testing.mfa import MFA_VERIFY, enroll_totp, fresh_totp_code


User = get_user_model()


@pytest.mark.django_db
def test_auth_login_sets_httponly_cookie_and_me_works_via_cookie():
    # "création par admin" simulée ici via ORM (équivalent à un user créé dans Django admin)
    u = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    device = enroll_totp(u)

    client = APIClient()

    # login email+password : le token n'est plus dans le JSON, il est en cookie HttpOnly.
    res = client.post(
        "/api/v1/auth/login/",
        {"email": "u1@example.com", "password": "pw"},
        format="json",
    )
    assert res.status_code == 200
    assert "token" not in res.data
    # Mot de passe seul : aucun cookie d'auth, /me refusé (MFA obligatoire).
    assert res.cookies.get(settings.AUTH_COOKIE_NAME) is None
    assert client.get("/api/v1/auth/me/").status_code == 401

    res = client.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 200
    assert "token" not in res.data
    cookie = res.cookies.get(settings.AUTH_COOKIE_NAME)
    assert cookie is not None and cookie.value
    assert cookie["httponly"]

    # endpoint protégé OK via le cookie d'auth (renvoyé automatiquement par le client)
    res_me = client.get("/api/v1/auth/me/")
    assert res_me.status_code == 200
    assert res_me.data.get("email") == "u1@example.com"


@pytest.mark.django_db
def test_auth_header_token_still_works_compat():
    # Compat : header Authorization: Token <key> accepté — pour un jeton post-MFA
    # seulement (un `authtoken.Token` est refusé, cf. test_cookie_auth).
    u = User.objects.create_user(username="u2", email="u2@example.com", password="pw")
    key = issue_auth_token(u, method="totp")

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {key}")
    res_me = client.get("/api/v1/auth/me/")
    assert res_me.status_code == 200
    assert res_me.data.get("email") == "u2@example.com"


@pytest.mark.django_db
def test_auth_me_requires_token():
    client = APIClient()
    res_me = client.get("/api/v1/auth/me/")
    assert res_me.status_code == 401  # IsAuthenticated -> 401 sans token


@pytest.mark.django_db
def test_no_public_register_endpoint():
    # création uniquement admin => pas de register public
    client = APIClient()
    res = client.post(
        "/api/v1/auth/register",
        {"email": "x@example.com", "password": "pw"},
        format="json",
    )
    assert res.status_code in (404, 405)
